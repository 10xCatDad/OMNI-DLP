"""Independent multiformat downloads verified with actual local media streams."""
from __future__ import annotations

from dataclasses import replace
import functools
import http.server
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from omni_dlp import merge
from omni_dlp.merge import component_media, download_merged, planned_paths
from omni_dlp.models import Format, Media, OmniError
from omni_dlp.transfer import TransferOptions, _destination_lock, download


def record(base="https://example.invalid"):
    return Media(base + "/watch", title="fixture", id="123", kind="video", source_url=base + "/watch",
                 requested_formats=[
                     Format(base + "/video.mp4", format_id="137", ext="mp4", vcodec="avc1", acodec="none"),
                     Format(base + "/audio.m4a", format_id="140", ext="m4a", vcodec="none", acodec="aac"),
                 ])


def test_planned_paths_match_components_and_preserve_template_directory(tmp_path):
    media = record()
    options = TransferOptions(directory=str(tmp_path), output="%(kind)s/%(title)s [%(id)s].%(ext)s")
    paths = planned_paths(media, options)
    assert [path.relative_to(tmp_path).as_posix() for path in paths] == [
        "video/fixture [123].f137.mp4", "video/fixture [123].f140.m4a", "video/fixture [123].mkv"]
    assert all(not path.exists() for path in paths)
    assert [item.kind for item in component_media(media)] == ["video", "audio"]


def test_component_origin_scoping_and_explicit_target_headers():
    media = record()
    media.headers = {"Authorization": "source-secret", "X-Api-Key": "source-key", "User-Agent": "fixture"}
    media.requested_formats[1].url = "https://cdn.invalid/audio.m4a"
    media.requested_formats[1].headers = {"Authorization": "cdn-specific"}
    first, second = component_media(media)
    assert first.headers["Authorization"] == "source-secret"
    assert second.headers == {"User-Agent": "fixture", "Authorization": "cdn-specific"}
    assert second.source_url == media.source_url


def test_components_have_independent_stable_resource_identity():
    media = record()
    media.metadata = {"archive_id": "site:123", "resource_id": "site:123|format:137+140"}
    first, second = component_media(media)
    assert first.metadata["resource_id"] == "site:123|format:137"
    assert second.metadata["resource_id"] == "site:123|format:140"
    media.metadata = {"resource_id": "unscoped-group"}
    assert "resource_id" not in component_media(media)[0].metadata


def test_filename_injections_and_duplicate_ids_stay_confined(tmp_path):
    media = record()
    media.title = "../../outside"
    for fmt in media.requested_formats:
        fmt.format_id = "../unsafe%id"
    paths = planned_paths(media, TransferOptions(directory=str(tmp_path)))
    assert len(set(paths)) == 3
    assert all(path.parent == tmp_path for path in paths)
    assert all("/" not in path.name for path in paths)
    assert ".2." in paths[1].name


def test_adaptive_component_plans_local_mkv(tmp_path):
    media = record()
    media.requested_formats[0] = replace(media.requested_formats[0], url="https://example.invalid/a.m3u8",
                                          ext="m3u8", protocol="hls")
    assert planned_paths(media, TransferOptions(directory=str(tmp_path)))[0].suffix == ".mkv"


@pytest.mark.parametrize("container,output", [("bad", "%(title)s.%(ext)s"), ("mkv", "../escape.mkv"),
                                                ("mkv", "invalid.doc")])
def test_invalid_outputs_fail_before_network(tmp_path, container, output):
    with patch("omni_dlp.merge.download") as transfer:
        with pytest.raises(OmniError):
            download_merged(record(), TransferOptions(directory=str(tmp_path), output=output), container=container)
        transfer.assert_not_called()


@pytest.mark.parametrize("url,protocol", [("file:///etc/passwd", "http"), ("https://example.invalid/a", "rtmp")])
def test_invalid_component_protocol_rejected(tmp_path, url, protocol):
    media = record()
    media.requested_formats[0] = replace(media.requested_formats[0], url=url, protocol=protocol)
    with patch("omni_dlp.merge.download") as transfer:
        with pytest.raises(OmniError):
            download_merged(media, TransferOptions(directory=str(tmp_path)))
        transfer.assert_not_called()


def test_precancelled_and_missing_ffmpeg_do_not_download(tmp_path):
    event = threading.Event()
    event.set()
    with patch("omni_dlp.merge.download") as transfer:
        with pytest.raises(OmniError, match="cancelled"):
            download_merged(record(), TransferOptions(directory=str(tmp_path), cancel_event=event))
        with patch("omni_dlp.streams.shutil.which", return_value=None):
            with pytest.raises(OmniError, match="install ffmpeg"):
                download_merged(record(), TransferOptions(directory=str(tmp_path)))
        transfer.assert_not_called()


@pytest.fixture(scope="module")
def media_server(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are not installed")
    root = tmp_path_factory.mktemp("merge-fixtures").resolve()
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=blue:s=64x64:r=10", "-t", "1.2", "-an", "-c:v", "libx264", "-threads", "1",
                    str(root / "video.mp4")], check=True, capture_output=True)
    for filename, frequency in (("audio.m4a", 440), ("changed.m4a", 880)):
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        f"sine=frequency={frequency}:sample_rate=48000", "-t", "1.2", "-vn", "-c:a", "aac",
                        str(root / filename)], check=True, capture_output=True)
    (root / "caption.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nFixture caption\n", encoding="utf-8")
    requests = []

    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            super().do_GET()

        def log_message(self, *_):
            pass

    handler = functools.partial(Handler, directory=str(root))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}", root, requests
    server.shutdown()
    server.server_close()
    worker.join()


def probe(path):
    response = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                              check=True, capture_output=True, text=True)
    return json.loads(response.stdout)


def test_real_merge_preserves_audio_video_and_components_with_verified_skip(media_server, tmp_path):
    base, _, requests = media_server
    media, options = record(base), TransferOptions(directory=str(tmp_path))
    count = len(requests)
    result = download_merged(media, options)
    paths = planned_paths(media, options)
    assert result.path == str(paths[-1])
    assert all(path.is_file() for path in paths)
    assert {stream["codec_type"] for stream in probe(result.path)["streams"]} == {"audio", "video"}
    assert len(requests) - count == 2
    with patch("omni_dlp.merge._execute") as execute:
        repeated = download_merged(media, options)
        execute.assert_not_called()
    assert repeated.skipped and repeated.sha256 == result.sha256
    assert len(requests) - count == 2


def test_explicit_container_extension_matches_actual_muxer(media_server, tmp_path):
    base, _, _ = media_server
    result = download_merged(record(base), TransferOptions(directory=str(tmp_path), output="chosen.mp4"))
    assert result.media.ext == "mp4"
    assert "mp4" in probe(result.path)["format"]["format_name"]


def test_changed_component_bytes_invalidate_merged_completion(media_server, tmp_path):
    base, source, _ = media_server
    media, options = record(base), TransferOptions(directory=str(tmp_path))
    first = download_merged(media, options)
    audio_path = planned_paths(media, options)[1]
    original = (source / "audio.m4a").read_bytes()
    try:
        (source / "audio.m4a").write_bytes((source / "changed.m4a").read_bytes())
        audio = component_media(media)[1]
        download(audio, replace(options, output=audio_path.name, overwrite=True))
        with pytest.raises(OmniError, match="matching completion record"):
            download_merged(media, options)
        updated = download_merged(media, replace(options, overwrite=True))
        assert updated.sha256 != first.sha256
        assert download_merged(media, options).skipped
    finally:
        (source / "audio.m4a").write_bytes(original)


def test_component_and_merge_locks_prevent_racing_writers(media_server, tmp_path):
    base, _, _ = media_server
    media, options = record(base), TransferOptions(directory=str(tmp_path))
    paths = planned_paths(media, options)
    with _destination_lock(paths[0]):
        with pytest.raises(OmniError, match="Another download"):
            download_merged(media, options)
    download_merged(media, options)
    with _destination_lock(paths[-1]):
        with pytest.raises(OmniError, match="Another download"):
            download_merged(media, options)


def test_maximum_merged_size_and_ffmpeg_errors_do_not_publish(media_server, tmp_path):
    base, _, _ = media_server
    media, options = record(base), TransferOptions(directory=str(tmp_path))
    parts = component_media(media)
    paths = planned_paths(media, options)
    results = [download(part, replace(options, output=path.name)) for part, path in zip(parts, paths)]
    # Existing verified component files are retained even when the merged
    # container exceeds a deliberately tiny final-output limit.
    with pytest.raises(OmniError, match="maximum file size"):
        download_merged(media, replace(options, max_filesize=1))
    assert not paths[-1].exists()
    assert all(Path(result.path).is_file() for result in results)
    media.requested_formats[1] = replace(media.requested_formats[1], vcodec="avc1")
    with pytest.raises(OmniError, match="FFmpeg reported an error"):
        download_merged(media, options)
    assert not paths[-1].exists()
    assert not list(tmp_path.glob(".omni-*"))


def test_secret_headers_are_scoped_and_receipts_do_not_retain_signed_urls(media_server, tmp_path):
    base, _, requests = media_server
    media = record(base)
    media.requested_formats[0].url += "?token=private-fixture-value"
    media.requested_formats[1].url = media.requested_formats[1].url.replace("127.0.0.1", "localhost")
    options = TransferOptions(directory=str(tmp_path), headers={"Authorization": "source-secret", "X-Api-Key": "source-key"})
    start = len(requests)
    download_merged(media, options)
    received = requests[start:]
    assert received[0][1]["Authorization"] == "source-secret"
    assert "Authorization" not in received[1][1] and "X-Api-Key" not in received[1][1]
    for path in tmp_path.glob("*.omni.json"):
        assert "private-fixture-value" not in path.read_text()
        assert "source-secret" not in path.read_text()
        assert base not in path.read_text()


def test_real_ffmpeg_cancellation_retains_components_and_no_merged_file(media_server, tmp_path):
    base, _, _ = media_server
    media = record(base)
    event, started = threading.Event(), threading.Event()
    options = TransferOptions(directory=str(tmp_path), cancel_event=event)
    processes, failures = [], []
    real_execute, real_popen = merge._execute, subprocess.Popen

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    def slow_execute(_arguments, temporary, limit=None, **kwargs):
        return real_execute(["-re", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "30",
                             "-c:a", "pcm_s16le", "-f", "matroska"], temporary, limit, **kwargs)

    def work():
        try:
            download_merged(media, options)
        except BaseException as exc:
            failures.append(exc)

    with patch("omni_dlp.merge._execute", side_effect=slow_execute), patch("omni_dlp.streams.subprocess.Popen", side_effect=spawn):
        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        try:
            assert started.wait(5)
            time.sleep(0.2)
            assert processes[0].poll() is None
            event.set()
            worker.join(3)
            assert not worker.is_alive()
            assert processes[0].poll() is not None
        finally:
            event.set()
            for process in processes:
                if process.poll() is None:
                    process.kill()
            worker.join(5)
    assert len(failures) == 1 and isinstance(failures[0], OmniError) and "cancelled" in str(failures[0])
    paths = planned_paths(media, options)
    assert all(path.is_file() for path in paths[:-1]) and not paths[-1].exists()
    assert not list(tmp_path.glob(".omni-*"))


def test_subtitle_component_is_preserved_as_a_subtitle_stream(media_server, tmp_path):
    base, _, _ = media_server
    media = record(base)
    media.requested_formats.append(Format(base + "/caption.srt", format_id="en", ext="srt",
                                         vcodec="none", acodec="none", metadata={"kind": "subtitle"}))
    result = download_merged(media, TransferOptions(directory=str(tmp_path)))
    assert {stream["codec_type"] for stream in probe(result.path)["streams"]} == {"video", "audio", "subtitle"}
    assert len(planned_paths(media, TransferOptions(directory=str(tmp_path)))) == 4
