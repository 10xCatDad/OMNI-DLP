from __future__ import annotations

import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from omni_dlp import cli


@pytest.fixture
def web():
    payloads = {
        "/photo.png": ("image/png", b"\x89PNG\r\n\x1a\nimage bytes"),
        "/sound.mp3": ("audio/mpeg", b"ID3audio bytes"),
        "/book.pdf": ("application/pdf", b"%PDF-1.7\ndocument bytes"),
        "/words.vtt": ("text/vtt", b"WEBVTT\n\n00:00.000 --> 00:01.000\nHello\n"),
        "/raw.json": ("application/json", b'{"count": 123, "metadata": "this is a data file"}\n'),
        "/raw.m3u8": ("application/vnd.apple.mpegurl", b'#EXTM3U\n#EXTINF:1,\nsegment.ts\n'),
        "/page": ("text/html", b'<html><img src="/photo.png?secret=hidden"><audio src="/sound.mp3"></audio><a href="/book.pdf">Book</a><track src="/words.vtt"></html>'),
    }
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def respond(self, body):
            path = self.path.split("?", 1)[0]
            requests.append((self.command, path))
            if path not in payloads:
                self.send_error(404)
                return
            mime, data = payloads[path]
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("ETag", '"fixture"')
            self.end_headers()
            if body:
                self.wfile.write(data)

        def do_HEAD(self):
            self.respond(False)

        def do_GET(self):
            self.respond(True)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payloads, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def invoke(arguments, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), patch("sys.stdin", io.StringIO(stdin)):
        try:
            status = cli.main(arguments)
        except SystemExit as exc:
            status = exc.code
    return status, out.getvalue(), err.getvalue()


def test_mixed_download_bytes_sidecars_and_manifest(web, tmp_path):
    base, payloads, _ = web
    manifest = tmp_path / "records" / "manifest.jsonl"
    output = tmp_path / "media"
    status, out, err = invoke([
        "download", base + "/page", "--no-plugins", "--jobs", "3", "-P", str(output),
        "-o", "%(kind)s/%(title)s.%(ext)s", "--write-info-json", "--manifest", str(manifest),
        "--progress-json", "--retries", "0",
    ])
    assert status == 0, err
    assert not out
    expected = {"image/photo.png": "/photo.png", "audio/sound.mp3": "/sound.mp3",
                "document/book.pdf": "/book.pdf", "subtitle/words.vtt": "/words.vtt"}
    for relative, source in expected.items():
        downloaded = output / relative
        assert downloaded.read_bytes() == payloads[source][1]
        sidecar = Path(str(downloaded) + ".info.json").read_text()
        assert "secret=hidden" not in sidecar
        assert "headers" not in json.loads(sidecar)["media"]
    records = [json.loads(line) for line in manifest.read_text().splitlines()]
    assert len(records) == 4
    assert all(record["size"] > 0 and len(record["sha256"]) == 64 for record in records)
    events = [json.loads(line) for line in err.splitlines()]
    assert len(events) == 4
    assert all(event["schema_version"] == 1 and event["event"] == "complete" for event in events)


@pytest.mark.parametrize("mode", [["inspect"], ["--dump-json"], ["--simulate"]])
def test_inspection_never_downloads_media_and_redacts_urls(web, tmp_path, mode):
    base, _, requests = web
    target = tmp_path / "not-created"
    status, out, err = invoke([*mode, base + "/page", "--no-plugins", "-P", str(target)])
    assert status == 0, err
    assert len(out.splitlines()) == 4
    assert "secret=hidden" not in out
    assert not target.exists()
    assert not any(method == "GET" and path != "/page" for method, path in requests)


def test_subtitle_filter_and_batch_stdin(web, tmp_path):
    base, _, _ = web
    status, out, err = invoke([
        "-a", "-", "--no-plugins", "--media-type", "subtitle", "--dump-json", "-P", str(tmp_path),
    ], stdin=f"# Sources\n\n{base}/page\n{base}/page\n")
    assert status == 0, err
    records = [json.loads(line) for line in out.splitlines()]
    assert len(records) == 1 and records[0]["kind"] == "subtitle"


def test_batch_file_config_defaults_and_cli_override(web, tmp_path):
    base, _, _ = web
    batch = tmp_path / "urls.txt"
    batch.write_text(f"# Comment\n{base}/page\n")
    config = tmp_path / "settings.toml"
    config.write_text('[omni]\nmedia_type = ["image"]\njobs = 2\nmax_items = 1\ndump_json = true\n')
    status, out, err = invoke(["--config", str(config), "-a", str(batch), "--max-items", "5", "--no-plugins"])
    assert status == 0, err
    assert len(out.splitlines()) == 1
    assert json.loads(out)["kind"] == "image"


@pytest.mark.parametrize("config", [
    'omni = "wrong"', 'omni = []', '[omni]\njobs = []', '[omni]\njobs = true',
    '[omni]\njobs = 1.5', '[omni]\nadd_header = [1]', '[omni]\ndirectory = {}',
    '[omni]\nmedia_type = ["imaginary"]', '[omni]\nquiet = "yes"',
    '[omni]\ntimeout = nan', '[omni]\nunknown = 1', '[omni]\ncookies = ["file"]',
])
def test_bad_configuration_is_a_clean_usage_error(tmp_path, config):
    path = tmp_path / "bad.toml"
    path.write_text(config)
    status, out, err = invoke(["--config", str(path), "https://example.invalid/"])
    assert status == 2
    assert "error:" in err
    assert "Traceback" not in err and not out


@pytest.mark.parametrize("timeout", ["nan", "inf", "-inf", "0"])
def test_nonfinite_or_nonpositive_timeouts_are_rejected(timeout):
    status, _, err = invoke([f"--socket-timeout={timeout}", "https://example.invalid/"])
    assert status == 2 and "timeout" in err


def test_compat_missing_dependency_is_a_clean_error():
    with patch("omni_dlp.compat.available", return_value=False):
        status, _, err = invoke(["compat", "--help"])
    assert status == 1 and "omni-dlp[compat]" in err and "Traceback" not in err


def test_compat_does_not_parse_upstream_flags():
    arguments = ["--new-upstream-option", "a b", "--help"]
    with patch("omni_dlp.compat.run", return_value=17) as run:
        status, _, _ = invoke(["compat", *arguments])
    assert status == 17
    run.assert_called_once_with(arguments)


def test_ignore_errors_still_returns_failure_and_downloads_success(web, tmp_path):
    base, payloads, _ = web
    status, out, err = invoke([
        base + "/missing", base + "/photo.png", "--ignore-errors", "--no-plugins",
        "--retries", "0", "-P", str(tmp_path), "-o", "%(title)s.%(ext)s",
    ])
    assert status == 1
    assert "404" in err
    assert (tmp_path / "photo.png").read_bytes() == payloads["/photo.png"][1]


def test_archive_repeat_verifies_bytes_and_detects_corruption(web, tmp_path):
    base, payloads, requests = web
    arguments = [base + "/photo.png", "--no-plugins", "-P", str(tmp_path / "media"),
                 "-o", "%(title)s.%(ext)s", "--download-archive", str(tmp_path / "archive.db")]
    assert invoke(arguments)[0] == 0
    requests.clear()
    status, out, err = invoke(arguments)
    assert status == 0 and "Verified" in out, err
    assert ("GET", "/photo.png") not in requests
    target = tmp_path / "media" / "photo.png"
    target.write_bytes(b"X" * len(payloads["/photo.png"][1]))
    status, _, err = invoke(arguments)
    assert status == 1 and "integrity check" in err
    assert invoke([*arguments, "--force-overwrites"])[0] == 0
    assert target.read_bytes() == payloads["/photo.png"][1]


def test_manifest_cannot_corrupt_media_output(web, tmp_path):
    base, _, requests = web
    target = tmp_path / "photo.png"
    status, _, err = invoke([base + "/photo.png", "--no-plugins", "-P", str(tmp_path),
                             "-o", "photo.png", "--manifest", str(target)])
    assert status == 1 and "overlaps" in err
    assert ("GET", "/photo.png") not in requests
    assert not target.exists()


def test_manifest_cannot_corrupt_archive_and_fatal_error_is_json(web, tmp_path):
    base, _, requests = web
    archive = tmp_path / "downloads.db"
    status, _, err = invoke([base + "/photo.png", "--no-plugins", "-P", str(tmp_path),
                             "--download-archive", str(archive), "--manifest", str(archive), "--progress-json"])
    assert status == 1
    event = json.loads(err)
    assert event["schema_version"] == 1 and event["event"] == "error"
    assert "overlaps" in event["error"]
    assert ("GET", "/photo.png") not in requests


@pytest.mark.parametrize("path", ["/raw.json", "/raw.m3u8"])
def test_direct_downloads_raw_structured_files_without_parsing(web, tmp_path, path):
    base, payloads, requests = web
    with patch("omni_dlp.engine.discover_plugin") as plugin, patch("omni_dlp.engine.discover") as discovery:
        status, _, err = invoke([base + path, "--direct", "-P", str(tmp_path), "-o", "%(title)s.%(ext)s"])
    assert status == 0, err
    assert (tmp_path / path.lstrip("/")).read_bytes() == payloads[path][1]
    plugin.assert_not_called()
    discovery.assert_not_called()
    assert requests == [("GET", path)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX SIGINT subprocess regression")
def test_sigint_cancels_slow_download_and_retains_partial(tmp_path):
    stopped = threading.Event()
    sent = threading.Event()

    class SlowHandler(BaseHTTPRequestHandler):
        def do_HEAD(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(100 * 1024 * 1024))
            self.end_headers()

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(100 * 1024 * 1024))
            self.send_header("ETag", '"slow-fixture"')
            self.end_headers()
            try:
                while not stopped.is_set():
                    self.wfile.write(b"x" * 1024)
                    self.wfile.flush()
                    sent.set()
                    stopped.wait(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), SlowHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(cli.__file__).resolve().parents[1])
    process = subprocess.Popen([
        sys.executable, "-m", "omni_dlp", f"http://127.0.0.1:{server.server_port}/slow.bin",
        "--no-plugins", "-P", str(tmp_path), "-o", "slow.bin", "--socket-timeout", "10",
    ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    try:
        assert sent.wait(5), "download did not start"
        partial = tmp_path / "slow.bin.part"
        deadline = time.monotonic() + 5
        while (not partial.exists() or not partial.stat().st_size) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert partial.exists() and partial.stat().st_size > 0
        started = time.monotonic()
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=4)
        assert process.returncode == 130, stderr
        assert time.monotonic() - started < 4
        assert "Interrupted" in stderr and "Traceback" not in stderr
        assert not stdout
        assert partial.is_file() and partial.stat().st_size > 0
        assert (tmp_path / "slow.bin.part.json").is_file()
        assert not (tmp_path / "slow.bin").exists()
    finally:
        stopped.set()
        if process.poll() is None:
            process.kill()
            process.communicate()
        server.shutdown()
        server.server_close()
        thread.join()
