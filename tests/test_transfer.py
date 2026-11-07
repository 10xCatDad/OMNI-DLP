"""Offline integration coverage for the actual HTTP transfer implementation."""
import hashlib
import http.server
import json
import socket
import tempfile
import threading
import unittest
from collections import namedtuple
from pathlib import Path
from unittest.mock import patch

from omni_dlp.models import Media, OmniError
from omni_dlp.transfer import TransferOptions, download, output_path, request_headers

DATA = bytes(range(256)) * 1024
OTHER = b"replacement" * 16384


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    requests = []
    count = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        route = self.path.split("?")[0]
        self.requests.append((route, dict(self.headers)))
        self.count[route] = self.count.get(route, 0) + 1
        count = self.count[route]
        if route == "/redirect":
            self.send_response(302)
            self.send_header("Location", f"http://localhost:{self.server.server_port}/blob")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route == "/same-redirect":
            self.send_response(302)
            self.send_header("Location", "/blob")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route == "/error":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if route in {"/retry", "/retry-invalid"} and count == 1:
            self.send_response(503)
            self.send_header("Retry-After", "nan" if route == "/retry-invalid" else "0")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = b"" if route == "/empty" else DATA
        etag = '"v1"'
        interrupted = route in {"/interrupt", "/ignore", "/changed", "/bad-range", "/bad-validator", "/range416", "/weak"}
        if route == "/changed" and count > 1:
            payload, etag = OTHER, '"v2"'
        if route == "/weak":
            etag = 'W/"v1"'
        range_header = self.headers.get("Range")
        if route == "/range416" and range_header:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{len(DATA)}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        status, start = 200, 0
        if range_header and route not in {"/ignore", "/changed"}:
            start = int(range_header.removeprefix("bytes=").removesuffix("-"))
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("ETag", '"wrong"' if route == "/bad-validator" and count > 1 else etag)
        if status == 206:
            advertised_start = 0 if route == "/bad-range" else start
            self.send_header("Content-Range", f"bytes {advertised_start}-{len(payload)-1}/{len(payload)}")
        self.send_header("Content-Length", str(len(payload) - start))
        self.end_headers()
        if interrupted and count == 1:
            self.wfile.write(payload[:32768])
            self.wfile.flush()
            self.close_connection = True
            self.connection.shutdown(socket.SHUT_WR)
            return
        try:
            self.wfile.write(payload[start:])
        except (BrokenPipeError, ConnectionResetError):
            pass


class TransferTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name).resolve()
        self.options = TransferOptions(directory=self.temp.name, retries=0)
        Handler.requests.clear()
        Handler.count.clear()

    def tearDown(self):
        self.temp.cleanup()

    def media(self, path="/blob"):
        return Media(url=self.base + path, title="fixture", id="test", ext="bin")

    def assert_bytes(self, result, expected=DATA):
        self.assertEqual(Path(result.path).read_bytes(), expected)
        self.assertEqual(result.size, len(expected))
        self.assertEqual(result.sha256, hashlib.sha256(expected).hexdigest())

    def partial(self, route):
        with self.assertRaisesRegex(OmniError, "partial data retained"):
            download(self.media(route), self.options)
        target = output_path(self.media(route), self.options)
        self.assertEqual(Path(str(target) + ".part").stat().st_size, 32768)
        self.assertFalse(target.exists())
        return target

    def test_byte_fidelity_atomic_completion_and_verified_skip(self):
        result = download(self.media(), self.options)
        self.assert_bytes(result)
        self.assertFalse(Path(result.path + ".part").exists())
        skipped = download(self.media(), self.options)
        self.assertTrue(skipped.skipped)
        self.assertEqual(len(Handler.requests), 1)

    def test_interrupted_transfer_resumes_with_validator(self):
        self.partial("/interrupt")
        result = download(self.media("/interrupt"), self.options)
        self.assert_bytes(result)
        self.assertEqual(Handler.requests[-1][1]["Range"], "bytes=32768-")
        self.assertEqual(Handler.requests[-1][1]["If-Range"], '"v1"')

    def test_interrupted_transfer_retries_in_same_call(self):
        self.options.retries = 1
        with patch("omni_dlp.transfer.time.sleep"):
            result = download(self.media("/interrupt"), self.options)
        self.assert_bytes(result)

    def test_range_ignored_restarts_instead_of_appending(self):
        self.partial("/ignore")
        result = download(self.media("/ignore"), self.options)
        self.assert_bytes(result)

    def test_changed_resource_restarts_instead_of_mixing_versions(self):
        self.partial("/changed")
        result = download(self.media("/changed"), self.options)
        self.assert_bytes(result, OTHER)

    def test_bad_range_does_not_corrupt_partial(self):
        target = self.partial("/bad-range")
        with self.assertRaisesRegex(OmniError, "unexpected byte range"):
            download(self.media("/bad-range"), self.options)
        self.assertEqual(Path(str(target) + ".part").read_bytes(), DATA[:32768])
        self.assertFalse(target.exists())

    def test_changed_validator_in_partial_response_is_rejected(self):
        self.partial("/bad-validator")
        with self.assertRaisesRegex(OmniError, "validator changed"):
            download(self.media("/bad-validator"), self.options)

    def test_weak_etag_does_not_resume(self):
        self.partial("/weak")
        self.assert_bytes(download(self.media("/weak"), self.options))
        self.assertNotIn("Range", Handler.requests[-1][1])

    def test_modified_partial_restarts(self):
        target = self.partial("/interrupt")
        Path(str(target) + ".part").write_bytes(b"x" * 32768)
        self.assert_bytes(download(self.media("/interrupt"), self.options))
        self.assertNotIn("Range", Handler.requests[-1][1])

    def test_disable_resume_restarts(self):
        self.partial("/interrupt")
        self.options.resume = False
        self.assert_bytes(download(self.media("/interrupt"), self.options))
        self.assertNotIn("Range", Handler.requests[-1][1])

    def test_416_retries_from_start(self):
        self.partial("/range416")
        self.options.retries = 1
        with patch("omni_dlp.transfer.time.sleep"):
            self.assert_bytes(download(self.media("/range416"), self.options))
        self.assertNotIn("Range", Handler.requests[-1][1])

    def test_transient_http_error_retries(self):
        self.options.retries = 1
        with patch("omni_dlp.transfer.time.sleep"):
            self.assert_bytes(download(self.media("/retry"), self.options))
        self.assertEqual(Handler.count["/retry"], 2)

    def test_invalid_retry_after_uses_finite_backoff(self):
        self.options.retries = 1
        with patch("omni_dlp.transfer.time.sleep") as sleep:
            self.assert_bytes(download(self.media("/retry-invalid"), self.options))
        self.assertEqual(sleep.call_args.args, (0.5,))

    def test_permanent_error_no_retry_or_secret_url(self):
        self.options.retries = 3
        with self.assertRaises(OmniError) as raised:
            download(self.media("/error?secret=do-not-log"), self.options)
        self.assertEqual(Handler.count["/error"], 1)
        self.assertNotIn("do-not-log", str(raised.exception))

    def test_empty_file_is_valid(self):
        self.assert_bytes(download(self.media("/empty"), self.options), b"")

    def test_unicode_url_is_requested_with_percent_encoding(self):
        self.assert_bytes(download(self.media("/café.png?title=你好&already=%20"), self.options))
        self.assertEqual(Handler.requests[-1][0], "/caf%C3%A9.png")

    def test_max_size_rejects_before_writing(self):
        self.options.max_filesize = 100
        with self.assertRaisesRegex(OmniError, "max-filesize"):
            download(self.media(), self.options)
        self.assertFalse(any(self.directory.glob("*.part")))

    def test_rate_limit_throttles(self):
        self.options.rate_limit = 1024
        with patch("omni_dlp.transfer.time.sleep") as sleep:
            self.assert_bytes(download(self.media(), self.options))
        self.assertGreater(sleep.call_count, 0)

    def test_insufficient_disk_space_rejects_before_writing(self):
        usage = namedtuple("usage", "total used free")(100, 99, 1)
        with patch("omni_dlp.transfer.shutil.disk_usage", return_value=usage):
            with self.assertRaisesRegex(OmniError, "free disk space"):
                download(self.media(), self.options)
        self.assertFalse(any(self.directory.glob("*.part")))

    def test_existing_unrelated_output_is_preserved(self):
        target = output_path(self.media(), self.options)
        target.write_bytes(b"user content")
        with self.assertRaisesRegex(OmniError, "already exists"):
            download(self.media(), self.options)
        self.assertEqual(target.read_bytes(), b"user content")
        self.assertFalse(Handler.requests)

    def test_existing_other_source_is_preserved(self):
        original = download(self.media(), self.options)
        with self.assertRaisesRegex(OmniError, "matching completion record"):
            download(self.media("/empty"), self.options)
        self.assert_bytes(original)

    def test_modified_completion_rejected(self):
        result = download(self.media(), self.options)
        Path(result.path).write_bytes(b"modified")
        with self.assertRaisesRegex(OmniError, "integrity check"):
            download(self.media(), self.options)

    def test_overwrite_is_explicit(self):
        target = output_path(self.media(), self.options)
        target.write_bytes(b"old")
        self.options.overwrite = True
        self.assert_bytes(download(self.media(), self.options))

    def test_unknown_partial_is_preserved(self):
        target = output_path(self.media(), self.options)
        part = Path(str(target) + ".part")
        part.write_bytes(b"unrelated")
        with self.assertRaisesRegex(OmniError, "unknown source"):
            download(self.media(), self.options)
        self.assertEqual(part.read_bytes(), b"unrelated")

    def test_completion_metadata_does_not_retain_url_or_headers(self):
        self.options.headers = {"Authorization": "Bearer private"}
        result = download(self.media("/blob?token=private"), self.options)
        marker = Path(result.path + ".omni.json").read_text()
        self.assertNotIn("private", marker)
        self.assertNotIn(self.base, marker)
        self.assertEqual(set(json.loads(marker)), {"version", "source_hash", "sha256", "size"})

    def test_cross_origin_redirect_drops_sensitive_and_custom_headers(self):
        self.options.headers = {"Authorization": "Bearer secret", "X-Api-Key": "secret", "Cookie": "session=secret"}
        self.assert_bytes(download(self.media("/redirect"), self.options))
        target = Handler.requests[-1][1]
        self.assertNotIn("Authorization", target)
        self.assertNotIn("X-Api-Key", target)
        self.assertNotIn("Cookie", target)

    def test_same_origin_redirect_keeps_authorization(self):
        self.options.headers = {"Authorization": "Bearer secret"}
        self.assert_bytes(download(self.media("/same-redirect"), self.options))
        self.assertEqual(Handler.requests[-1][1]["Authorization"], "Bearer secret")

    def test_cross_origin_discovered_media_drops_supplied_secrets(self):
        media = self.media()
        media.source_url = "https://original.example.test/gallery"
        self.options.headers = {"Authorization": "Bearer secret", "X-Api-Key": "secret", "Accept": "*/*"}
        self.assert_bytes(download(media, self.options))
        target = Handler.requests[-1][1]
        self.assertNotIn("Authorization", target)
        self.assertNotIn("X-Api-Key", target)
        self.assertEqual(target["Accept"], "*/*")

    def test_cookie_file_is_loaded(self):
        cookies = self.directory / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t2147483647\tsession\tfixture\n")
        self.options.cookies = str(cookies)
        self.assert_bytes(download(self.media(), self.options))
        self.assertEqual(Handler.requests[-1][1]["Cookie"], "session=fixture")

    def test_metadata_path_injection_is_sanitized(self):
        media = self.media()
        media.title, media.id, media.ext = "../../escape", "../identifier", "../jpg"
        path = output_path(media, self.options)
        self.assertEqual(path.parent, self.directory)
        self.assertNotIn("/", path.name)

    def test_nested_template_and_numeric_field(self):
        self.options.output = "%(kind)s/%(playlist_index)03d-%(title)s.%(ext)s"
        media = self.media()
        media.metadata["playlist_index"] = 7
        path = output_path(media, self.options)
        self.assertEqual(path.relative_to(self.directory).as_posix(), "other/007-fixture.bin")

    def test_percent_in_title_and_literal_template_is_preserved(self):
        media = self.media()
        media.title = "100% original"
        self.options.output = "%%-%(title)s.%(ext)s"
        self.assertEqual(output_path(media, self.options).name, "%-100% original.bin")

    def test_traversal_absolute_and_invalid_templates_rejected(self):
        for template in ["../outside", "/absolute", "a/../../outside", "C:\\outside", "%(unknown)s", "%(title)9999d", "line\nname", "bad?/name"]:
            with self.subTest(template=template):
                self.options.output = template
                with self.assertRaises(OmniError):
                    output_path(self.media(), self.options)

    def test_symlink_output_and_sidecar_rejected(self):
        target = output_path(self.media(), self.options)
        external = self.directory / "external"
        external.write_bytes(b"preserve")
        for suffix in ["", ".part", ".part.json", ".omni.json", ".omni.lock"]:
            link = Path(str(target) + suffix)
            try:
                link.symlink_to(external)
            except OSError:
                self.skipTest("Symlinks unavailable")
            with self.assertRaisesRegex(OmniError, "symbolic-link"):
                download(self.media(), self.options)
            link.unlink()
        self.assertEqual(external.read_bytes(), b"preserve")

    def test_symlink_parent_rejected(self):
        folder = self.directory / "folder"
        folder.mkdir()
        try:
            (self.directory / "link").symlink_to(folder, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks unavailable")
        self.options.output = "link/file.bin"
        with self.assertRaisesRegex(OmniError, "symbolic-link"):
            output_path(self.media(), self.options)

    def test_invalid_url_and_header_inputs(self):
        for url in ["file:///etc/passwd", "http://user:password@example.test/a", "http://example.test/a\nInjected", "http://[broken"]:
            with self.subTest(url=url), self.assertRaises(OmniError):
                download(Media(url=url), self.options)
        for headers in [{"X-Test": "bad\r\ninjected"}, {"Range": "bytes=0-"}, {"Bad Header": "x"}]:
            with self.subTest(headers=headers), self.assertRaises(OmniError):
                request_headers(headers)

    def test_invalid_options(self):
        for key, value in [("retries", -1), ("timeout", 0), ("rate_limit", 0), ("max_filesize", -1), ("timeout", float("nan")), ("timeout", float("inf")), ("retries", 1.5), ("rate_limit", 1.5)]:
            with self.subTest(key=key), self.assertRaises(OmniError):
                download(self.media(), TransferOptions(directory=self.temp.name, **{key: value}))

    def test_pre_cancelled_download_makes_no_request(self):
        self.options.cancel_event = threading.Event()
        self.options.cancel_event.set()
        with self.assertRaisesRegex(OmniError, "Download cancelled"):
            download(self.media(), self.options)
        self.assertFalse(Handler.requests)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cancel_during_rate_wait_retains_resumable_partial(self):
        event = threading.Event()
        self.options.cancel_event = event
        self.options.rate_limit = 1024

        def cancel(_delay):
            event.set()
            return True

        with patch.object(event, "wait", side_effect=cancel):
            with self.assertRaisesRegex(OmniError, "Download cancelled"):
                download(self.media(), self.options)
        target = output_path(self.media(), self.options)
        part = Path(str(target) + ".part")
        size = part.stat().st_size
        self.assertGreater(size, 0)
        self.assertLess(size, len(DATA))
        state = json.loads(Path(str(target) + ".part.json").read_text())
        self.assertEqual(state["sha256"], hashlib.sha256(part.read_bytes()).hexdigest())
        self.assertFalse(target.exists())
        event.clear()
        self.options.rate_limit = None
        self.assert_bytes(download(self.media(), self.options))
        self.assertEqual(Handler.requests[-1][1]["Range"], f"bytes={size}-")

    def test_cancel_interrupts_retry_wait(self):
        event = threading.Event()
        self.options.cancel_event = event
        self.options.retries = 3

        def cancel(_delay):
            event.set()
            return True

        with patch.object(event, "wait", side_effect=cancel):
            with self.assertRaisesRegex(OmniError, "Download cancelled"):
                download(self.media("/retry"), self.options)
        self.assertEqual(Handler.count["/retry"], 1)


if __name__ == "__main__":
    unittest.main()
