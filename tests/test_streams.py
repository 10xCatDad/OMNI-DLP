"""Real adaptive protocol tests with generated fixtures; no external service."""
from __future__ import annotations

import functools
import http.server
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from omni_dlp.models import Media, OmniError
from omni_dlp.streams import download_stream, transcode
from omni_dlp.transfer import TransferOptions, _destination_lock, output_path


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class StreamValidationTests(unittest.TestCase):
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

    def test_missing_segment_does_not_commit_partial_success(self):
        broken = self.root / "broken.m3u8"
        original = (self.root / "stream.m3u8").read_text()
        broken.write_text(original.replace("stream1.ts", "does-not-exist.ts"))
        with self.assertRaises(OmniError):
            download_stream(self.media("broken.m3u8"), self.options)
        self.assert_no_output()


if __name__ == "__main__":
    unittest.main()
