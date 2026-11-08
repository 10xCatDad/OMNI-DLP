"""Real adaptive protocol tests with generated fixtures; no external service."""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omni_dlp.models import Media, OmniError
from omni_dlp.streams import download_stream, transcode
from omni_dlp import streams as stream_module
from omni_dlp.transfer import TransferOptions, _destination_lock, download, output_path


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class StreamValidationTests(unittest.TestCase):
    def test_exact_benign_hls_mime_diagnostic_is_allowed_only_on_success(self):
        accepted = [
            b"mime type is not rfc8216 compliant\n",
            b"[hls @ 0x123abc] mime type is not rfc8216 compliant\n",
            b"[hls @ 000001ABCDEF] mime type is not rfc8216 compliant\r\n",
        ]
        for diagnostic in accepted:
            with self.subTest(diagnostic=diagnostic), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "result.mkv"
                target.write_bytes(b"fixture-media")
                def spawn(*_args, **kwargs):
                    kwargs["stderr"].write(diagnostic)
                    process = unittest.mock.Mock()
                    process.wait.return_value = process.poll.return_value = 0
                    return process
                with patch("omni_dlp.streams.subprocess.Popen", side_effect=spawn), patch("omni_dlp.streams._ffmpeg", return_value="ffmpeg"):
                    stream_module._execute([], target, strict=True)

    def test_benign_mime_warning_does_not_hide_failures_or_other_diagnostics(self):
        cases = [
            (1, True, b"mime type is not rfc8216 compliant\n"),
            (0, False, b"mime type is not rfc8216 compliant\n"),
            (0, True, b"[hls @ 0x123] mime type is not rfc8216 compliant; missing segment\n"),
            (0, True, b"[http @ 0x123] mime type is not rfc8216 compliant\n"),
            (0, True, b"mime type is not rfc8216 compliant\nFailed to open segment\n"),
            (0, True, b"mime type is not rfc8216 compliant\n\n"),
            (0, True, b"mime type is not rfc8216 compliant\n" * 3000),
        ]
        for code, strict, diagnostic in cases:
            with self.subTest(code=code, strict=strict, diagnostic=diagnostic[:80]), tempfile.TemporaryDirectory() as folder:
                target = Path(folder) / "result.mkv"
                target.write_bytes(b"fixture-media")
                def spawn(*_args, **kwargs):
                    kwargs["stderr"].write(diagnostic)
                    process = unittest.mock.Mock()
                    process.wait.return_value = process.poll.return_value = code
                    return process
                with patch("omni_dlp.streams.subprocess.Popen", side_effect=spawn), patch("omni_dlp.streams._ffmpeg", return_value="ffmpeg"):
                    with self.assertRaises(OmniError):
                        stream_module._execute([], target, strict=strict)

    def test_rejects_credentials_and_untrusted_protocols(self):
        for url in ("file:///tmp/private", "https://user:secret@example.com/live.m3u8"):
            with self.subTest(url=url), self.assertRaises(OmniError):
                download_stream(Media(url))

    def test_rejects_authentication_header_and_cookie_forwarding(self):
        for options in (TransferOptions(headers={"Authorization": "secret"}), TransferOptions(cookies="cookies.txt")):
            with self.assertRaisesRegex(OmniError, "compatibility mode"):
                download_stream(Media("https://example.com/live.m3u8"), options)

    def test_rate_limit_is_not_silently_ignored(self):
        with self.assertRaisesRegex(OmniError, "rate limits"):
            download_stream(Media("https://example.com/live.m3u8"), TransferOptions(rate_limit=100))

    def test_drm_is_not_reported_as_supported(self):
        with self.assertRaisesRegex(OmniError, "DRM"):
            download_stream(Media("https://example.com/live.mpd", metadata={"drm": True}))

    def test_missing_ffmpeg_is_actionable(self):
        with tempfile.TemporaryDirectory() as folder, patch("omni_dlp.streams.shutil.which", return_value=None):
            with self.assertRaisesRegex(OmniError, "install ffmpeg"):
                download_stream(Media("https://example.com/live.m3u8", id="test", title="test"), TransferOptions(directory=folder))

    def test_precancelled_download_does_not_start_ffmpeg(self):
        cancelled = threading.Event()
        cancelled.set()
        with patch("omni_dlp.streams.subprocess.Popen") as popen:
            with self.assertRaisesRegex(OmniError, "cancelled"):
                download_stream(Media("https://example.com/live.m3u8"), TransferOptions(cancel_event=cancelled))
            popen.assert_not_called()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is not installed")
class AdaptiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory()
        cls.root = Path(cls.fixture.name)
        base = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
            "-t", "1.2", "-c:v", "libx264", "-threads", "1", "-g", "5", "-c:a", "aac",
        ]
        subprocess.run(base + ["-f", "hls", "-hls_time", "0.5", "-hls_playlist_type", "vod", str(cls.root / "stream.m3u8")], check=True, capture_output=True)
        subprocess.run(base + ["-f", "dash", "-seg_duration", "0.5", str(cls.root / "stream.mpd")], check=True, capture_output=True)
        # Public fixture keys are temporary random bytes, never source assets.
        for suffix, byte, iv in (("a", 17, "00000000000000000000000000000001"),
                                 ("b", 34, "00000000000000000000000000000002"),
                                 ("c", 51, None)):
            key = cls.root / ("key-" + suffix + ".bin")
            key.write_bytes(bytes([byte]) * 16)
            info = cls.root / ("key-" + suffix + ".txt")
            info.write_text(key.name + "\n" + str(key) + "\n" + (iv + "\n" if iv else ""))
            subprocess.run(base + ["-f", "hls", "-hls_time", "0.5", "-hls_playlist_type", "vod",
                "-hls_key_info_file", str(info), str(cls.root / ("encrypted-" + suffix + ".m3u8"))],
                check=True, capture_output=True)
        handler = functools.partial(QuietHandler, directory=cls.fixture.name)
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.fixture.cleanup()

    def setUp(self):
        self.output = tempfile.TemporaryDirectory()
        self.addCleanup(self.output.cleanup)
        self.options = TransferOptions(directory=self.output.name)

    def media(self, manifest):
        return Media(self.base_url + "/" + manifest, title="sample", id=manifest, kind="video", ext=manifest.rsplit(".", 1)[1])

    def probe(self, path):
        process = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)], check=True, capture_output=True, text=True)
        return json.loads(process.stdout)["streams"]

    def assert_no_output(self):
        self.assertEqual([p for p in Path(self.output.name).iterdir() if not p.name.endswith(".omni.lock")], [])

    def assert_cancelled_process(self, call, cancelled):
        """Run a real slow FFmpeg fixture through the public operation's lifecycle."""
        real_execute = stream_module._execute
        real_popen = subprocess.Popen
        started = threading.Event()
        processes = []
        failures = []

        def spawn(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        def slow_fixture(_arguments, temporary, _limit=None, **kwargs):
            self.assertIs(kwargs.get("cancel_event"), cancelled)
            # A generated, realtime input avoids depending on a live broadcaster.
            return real_execute([
                "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
                "-t", "30", "-c:a", "pcm_s16le", "-f", "matroska",
            ], temporary, cancel_event=kwargs.get("cancel_event"))

        def run():
            try:
                call()
            except BaseException as exc:
                failures.append(exc)

        with patch("omni_dlp.streams.subprocess.Popen", side_effect=spawn), patch("omni_dlp.streams._execute", side_effect=slow_fixture):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(started.wait(5), "FFmpeg did not start")
                time.sleep(0.2)
                self.assertIsNone(processes[0].poll(), "Fixture finished before cancellation")
                start = time.monotonic()
                cancelled.set()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "Cancellation did not stop FFmpeg promptly")
                self.assertLess(time.monotonic() - start, 3)
                self.assertIsNotNone(processes[0].poll())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], OmniError)
                self.assertIn("cancelled", str(failures[0]))
            finally:
                cancelled.set()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                worker.join(5)

    def test_adaptive_cancellation_kills_process_and_cleans_partial_output(self):
        cancelled = threading.Event()
        options = TransferOptions(directory=self.output.name, cancel_event=cancelled)
        self.assert_cancelled_process(lambda: download_stream(self.media("stream.m3u8"), options), cancelled)
        self.assert_no_output()

    def test_conversion_cancellation_retains_source_and_cleans_partial_output(self):
        source = download_stream(self.media("stream.m3u8"), self.options)
        cancelled = threading.Event()
        self.assert_cancelled_process(lambda: transcode(source, audio_format="flac", cancel_event=cancelled), cancelled)
        self.assertTrue(Path(source.path).is_file())
        self.assertFalse(Path(source.path).with_suffix(".flac").exists())
        self.assertEqual(list(Path(self.output.name).glob(".omni-*")), [])

    def test_hls_and_dash_preserve_audio_video_and_verified_repeat(self):
        for manifest in ("stream.m3u8", "stream.mpd"):
            with self.subTest(manifest=manifest):
                result = download_stream(self.media(manifest), self.options)
                self.assertGreater(result.size, 0)
                self.assertEqual(result.media.ext, "mkv")
                self.assertEqual({stream["codec_type"] for stream in self.probe(result.path)}, {"audio", "video"})
                repeated = download_stream(self.media(manifest), self.options)
                self.assertTrue(repeated.skipped)
                self.assertEqual(repeated.sha256, result.sha256)

    def test_audio_conversion_and_remux_retain_source(self):
        result = download_stream(self.media("stream.m3u8"), self.options)
        audio = transcode(result, audio_format="flac")
        self.assertEqual({stream["codec_type"] for stream in self.probe(audio.path)}, {"audio"})
        self.assertEqual(audio.media.kind, "audio")
        self.assertTrue(Path(result.path).is_file())
        remux = transcode(result, remux_format="mp4")
        self.assertEqual({stream["codec_type"] for stream in self.probe(remux.path)}, {"audio", "video"})
        self.assertTrue(Path(result.path).is_file())

    def test_aiff_and_m4b_audio_roundtrip(self):
        for extension, codec in (("aiff", "pcm_s16be"), ("m4b", "aac")):
            with self.subTest(extension=extension):
                result = download_stream(self.media("stream.m3u8"), TransferOptions(directory=str(Path(self.output.name) / extension)))
                audio = transcode(result, audio_format=extension)
                streams = self.probe(audio.path)
                self.assertEqual([(s["codec_type"], s["codec_name"]) for s in streams], [("audio", codec)])
                roundtrip = transcode(audio, audio_format="wav")
                self.assertEqual(self.probe(roundtrip.path)[0]["codec_name"], "pcm_s16le")
                self.assertTrue(Path(result.path).is_file())

    def test_discovery_headers_are_accepted(self):
        media = self.media("stream.m3u8")
        media.headers = {"User-Agent": "omni-dlp/0.1", "Accept-Encoding": "identity"}
        result = download_stream(media, self.options)
        self.assertEqual({stream["codec_type"] for stream in self.probe(result.path)}, {"audio", "video"})

    def test_explicit_output_container_is_honored(self):
        result = download_stream(self.media("stream.m3u8"), TransferOptions(directory=self.output.name, output="selected.mp4"))
        self.assertEqual(result.media.ext, "mp4")
        process = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-of", "json", result.path], check=True, capture_output=True, text=True)
        self.assertIn("mp4", json.loads(process.stdout)["format"]["format_name"])

    def test_raw_manifest_cannot_be_skipped_as_completed_adaptive_media(self):
        media = self.media("stream.m3u8")
        options = TransferOptions(directory=self.output.name, output="shared.mkv")
        raw = download(media, options)
        self.assertTrue(Path(raw.path).read_bytes().startswith(b"#EXTM3U"))
        with self.assertRaisesRegex(OmniError, "matching completion record"):
            download_stream(media, options)
        adaptive = download_stream(media, replace(options, overwrite=True))
        self.assertFalse(adaptive.skipped)
        self.assertEqual({stream["codec_type"] for stream in self.probe(adaptive.path)}, {"audio", "video"})
        self.assertTrue(download_stream(media, options).skipped)
        with self.assertRaisesRegex(OmniError, "matching completion record"):
            download(media, options)

    def test_size_limit_does_not_commit_output(self):
        with self.assertRaisesRegex(OmniError, "maximum file size"):
            download_stream(self.media("stream.m3u8"), TransferOptions(directory=self.output.name, max_filesize=100))
        self.assert_no_output()

    def test_hostile_title_is_confined_and_receipt_hides_signed_url(self):
        media = self.media("stream.m3u8?token=fixture-private-value")
        media.title = "../../outside/private"
        result = download_stream(media, self.options)
        self.assertEqual(Path(result.path).parent, Path(self.output.name))
        receipt = Path(result.path + ".omni.json").read_text()
        self.assertNotIn("fixture-private-value", receipt)
        self.assertNotIn(media.url, receipt)
        self.assertNotIn(media.title, receipt)

    def test_corrupted_existing_output_does_not_skip(self):
        media = self.media("stream.m3u8")
        result = download_stream(media, self.options)
        Path(result.path).write_bytes(b"corrupted")
        with self.assertRaises(OmniError):
            download_stream(media, self.options)

    def test_changed_conversion_input_requires_explicit_overwrite(self):
        result = download_stream(self.media("stream.m3u8"), self.options)
        original = transcode(result, audio_format="flac")
        repeated = transcode(result, audio_format="flac")
        self.assertTrue(repeated.skipped)
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
            "-i", "sine=frequency=880:sample_rate=48000", "-t", "1.2", "-c:a", "pcm_s16le", result.path,
        ], check=True, capture_output=True)
        with self.assertRaisesRegex(OmniError, "matching completion record"):
            transcode(result, audio_format="flac")
        updated = transcode(result, audio_format="flac", overwrite=True)
        self.assertFalse(updated.skipped)
        self.assertNotEqual(updated.sha256, original.sha256)
        self.assertTrue(transcode(result, audio_format="flac").skipped)

    def test_adaptive_and_conversion_respect_destination_locks(self):
        media = self.media("stream.m3u8")
        target = output_path(replace(media, ext="mkv"), self.options)
        with _destination_lock(target):
            with self.assertRaisesRegex(OmniError, "Another download"):
                download_stream(media, self.options)
        result = download_stream(media, self.options)
        with _destination_lock(Path(result.path).with_suffix(".flac")):
            with self.assertRaisesRegex(OmniError, "Another download"):
                transcode(result, audio_format="flac")
        with _destination_lock(Path(result.path)):
            with self.assertRaisesRegex(OmniError, "Another download"):
                transcode(result, audio_format="flac")
        self.assertTrue(Path(result.path).is_file())

    def test_explicit_output_traversal_is_rejected(self):
        with self.assertRaises(OmniError):
            download_stream(self.media("stream.m3u8"), TransferOptions(directory=self.output.name, output="../escape.mkv"))

    def test_failure_does_not_expose_url_tokens(self):
        with self.assertRaises(OmniError) as raised:
            download_stream(self.media("missing.m3u8?token=fixture-private-value"), self.options)
        self.assertNotIn("fixture-private-value", str(raised.exception))
        self.assertNotIn(self.base_url, str(raised.exception))

    def test_local_file_manifest_segment_is_blocked(self):
        forbidden = self.root / "forbidden.m3u8"
        forbidden.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:10\n#EXTINF:10,\n" + (self.root / "stream0.ts").as_uri() + "\n#EXT-X-ENDLIST\n")
        with self.assertRaises(OmniError):
            download_stream(self.media("forbidden.m3u8"), self.options)
        self.assert_no_output()

    def test_valid_hls_with_octet_stream_mime_downloads_successfully(self):
        class OctetStreamHandler(QuietHandler):
            def guess_type(self, path):
                if path.endswith(".m3u8"):
                    return "application/octet-stream"
                return super().guess_type(path)
        handler = functools.partial(OctetStreamHandler, directory=str(self.root))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            media = self.media("stream.m3u8")
            media.url = f"http://127.0.0.1:{server.server_port}/stream.m3u8"
            result = download_stream(media, self.options)
            self.assertEqual({item["codec_type"] for item in self.probe(result.path)}, {"audio", "video"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_ordinary_aes128_hls_download_decodes_audio_and_video(self):
        result = download_stream(self.media("encrypted-a.m3u8"), self.options)
        self.assertEqual({item["codec_type"] for item in self.probe(result.path)}, {"audio", "video"})
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", result.path, "-f", "null", "-"], capture_output=True)
        self.assertEqual(decoded.returncode, 0, decoded.stderr.decode())
        self.assertEqual(decoded.stderr, b"")
        self.assertEqual(result.media.metadata["encryption"], "AES-128")
        receipt = Path(result.path + ".omni.json").read_text()
        self.assertNotIn("key-a.bin", receipt)
        self.assertTrue(download_stream(self.media("encrypted-a.m3u8"), self.options).skipped)

    def test_aes128_implicit_sequence_number_iv(self):
        import re
        original = (self.root / "encrypted-c.m3u8").read_text()
        implicit = re.sub(r",IV=0x[0-9a-fA-F]+", "", original)
        (self.root / "implicit-iv.m3u8").write_text(implicit)
        result = download_stream(self.media("implicit-iv.m3u8"), self.options)
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", result.path, "-f", "null", "-"], capture_output=True)
        self.assertEqual(decoded.returncode, 0)
        self.assertEqual(decoded.stderr, b"")
        self.assertEqual({item["codec_type"] for item in self.probe(result.path)}, {"audio", "video"})

    def test_aes128_key_rotation_and_explicit_iv(self):
        first = (self.root / "encrypted-a.m3u8").read_text().splitlines()
        second = (self.root / "encrypted-b.m3u8").read_text().splitlines()
        key_b = next(line for line in second if line.startswith("#EXT-X-KEY:"))
        first_segment = first.index("encrypted-a0.ts")
        remaining = second[second.index("encrypted-b0.ts") + 1:]
        rotating = "\n".join(first[:first_segment + 1] + [key_b] + remaining) + "\n"
        (self.root / "rotating.m3u8").write_text(rotating)
        result = download_stream(self.media("rotating.m3u8"), self.options)
        self.assertEqual({item["codec_type"] for item in self.probe(result.path)}, {"audio", "video"})
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-i", result.path, "-f", "null", "-"], capture_output=True)
        self.assertEqual(decoded.returncode, 0)
        self.assertEqual(decoded.stderr, b"")

    def test_aes128_missing_and_wrong_length_keys_fail_without_output(self):
        original = (self.root / "encrypted-a.m3u8").read_text()
        (self.root / "short-key.bin").write_bytes(b"short")
        for index, name in enumerate(("missing-key.bin", "short-key.bin")):
            manifest = f"missing-key-{index}.m3u8"
            (self.root / manifest).write_text(original.replace("key-a.bin", name))
            with self.subTest(name=name), self.assertRaisesRegex(OmniError, "AES-128 key"):
                download_stream(self.media(manifest), self.options)
            self.assert_no_output()

    def test_wrong_aes128_key_does_not_publish_corrupt_media(self):
        (self.root / "wrong-key.bin").write_bytes(bytes(16))
        original = (self.root / "encrypted-a.m3u8").read_text()
        (self.root / "wrong-key.m3u8").write_text(original.replace("key-a.bin", "wrong-key.bin"))
        with self.assertRaises(OmniError):
            download_stream(self.media("wrong-key.m3u8"), self.options)
        self.assert_no_output()

    def test_unsafe_key_protocols_are_rejected_before_ffmpeg_starts(self):
        original = (self.root / "encrypted-a.m3u8").read_text()
        for key_url in ((self.root / "key-a.bin").as_uri(), "data:text/plain,example", "crypto:file:///key", "tcp://localhost:9"):
            (self.root / "unsafe-key.m3u8").write_text(original.replace("key-a.bin", key_url))
            with self.subTest(key_url=key_url), patch("omni_dlp.streams._execute") as execute:
                with self.assertRaises(OmniError):
                    download_stream(self.media("unsafe-key.m3u8"), self.options)
                execute.assert_not_called()
            self.assert_no_output()

    def test_sample_aes_is_rejected_before_ffmpeg_starts(self):
        original = (self.root / "encrypted-a.m3u8").read_text()
        (self.root / "sample-aes.m3u8").write_text(original.replace("METHOD=AES-128", "METHOD=SAMPLE-AES"))
        with patch("omni_dlp.streams._execute") as execute:
            with self.assertRaisesRegex(OmniError, "Unsupported HLS encryption"):
                download_stream(self.media("sample-aes.m3u8"), self.options)
            execute.assert_not_called()
        self.assert_no_output()

    def test_master_aes128_preflight_and_crypto_only_when_needed(self):
        (self.root / "encrypted-master.m3u8").write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100000,RESOLUTION=64x64\nencrypted-a.m3u8\n')
        real_execute = stream_module._execute
        commands = []
        def execute(arguments, *args, **kwargs):
            commands.append(arguments)
            return real_execute(arguments, *args, **kwargs)
        with patch("omni_dlp.streams._execute", side_effect=execute):
            result = download_stream(self.media("encrypted-master.m3u8"), self.options)
            download_stream(self.media("stream.m3u8"), self.options)
        self.assertIn("crypto", commands[0][1].split(','))
        self.assertNotIn("crypto", commands[1][1].split(','))
        self.assertEqual(result.media.metadata["encryption"], "AES-128")

    def test_missing_segment_does_not_commit_partial_success(self):
        broken = self.root / "broken.m3u8"
        original = (self.root / "stream.m3u8").read_text()
        broken.write_text(original.replace("stream1.ts", "does-not-exist.ts"))
        with self.assertRaises(OmniError):
            download_stream(self.media("broken.m3u8"), self.options)
        self.assert_no_output()


if __name__ == "__main__":
    unittest.main()
