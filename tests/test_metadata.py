"""Native metadata embedding is checked against actual FFmpeg/ffprobe outputs."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from unittest.mock import patch

import pytest

from omni_dlp import metadata as operation
from omni_dlp.metadata import embed_metadata, plan_output
from omni_dlp.models import DownloadResult, Media, OmniError
from omni_dlp.transfer import _destination_lock


def result(path, *, title="New title", kind="video", metadata=None):
    path = Path(path)
    return DownloadResult(Media("https://example.invalid/media?token=secret-fixture", title=title,
                                id="fixture", ext=path.suffix.lstrip("."), kind=kind,
                                headers={"Authorization": "Bearer fixture-private"}, metadata=metadata or {}),
                          str(path), path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest())


def probe(path):
    response = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-show_format",
                               "-of", "json", str(path)], check=True, capture_output=True, text=True)
    return json.loads(response.stdout)


def tags(info):
    values = {key.lower(): value for key, value in info.get("format", {}).get("tags", {}).items()}
    for stream in info["streams"]:
        for key, value in stream.get("tags", {}).items():
            values.setdefault(key.lower(), value)
    return values


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe are not installed")
    root = tmp_path_factory.mktemp("metadata-fixtures")
    base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    subprocess.run(base + ["-f", "lavfi", "-i", "color=c=blue:s=64x64:r=10", "-f", "lavfi", "-i",
                           "sine=frequency=440:sample_rate=48000", "-t", "2", "-c:v", "libx264", "-threads", "1",
                           "-c:a", "aac", "-metadata", "title=Original title", "-metadata", "comment=Preserved source note",
                           str(root / "source.mp4")], check=True, capture_output=True)
    subprocess.run(base + ["-i", str(root / "source.mp4"), "-map", "0", "-c", "copy", str(root / "source.mkv")],
                   check=True, capture_output=True)
    for extension, codec in (("mp3", "libmp3lame"), ("flac", "flac"), ("ogg", "libvorbis"),
                             ("opus", "libopus"), ("wav", "pcm_s16le"), ("aiff", "pcm_s16be")):
        subprocess.run(base + ["-i", str(root / "source.mp4"), "-vn", "-c:a", codec, str(root / ("source." + extension))],
                       check=True, capture_output=True)
    (root / "original.srt").write_text("1\n00:00:00,000 --> 00:00:01,800\nOriginal caption\n", encoding="utf-8")
    (root / "added.srt").write_text("1\n00:00:00,000 --> 00:00:01,800\nAdded caption\n", encoding="utf-8")
    (root / "added.vtt").write_text("WEBVTT\n\n00:00:00.000 --> 00:00:01.800\nAdded caption\n", encoding="utf-8")
    for extension, codec in (("mp4", "mov_text"), ("mkv", "copy")):
        subprocess.run(base + ["-i", str(root / ("source." + extension)), "-i", str(root / "original.srt"),
                               "-map", "0", "-map", "1:s:0", "-c", "copy", "-c:s", codec,
                               "-metadata:s:s:0", "language=deu", str(root / ("subbed." + extension))],
                       check=True, capture_output=True)
    return root


def source(fixtures, tmp_path, name="source.mp4", **kwargs):
    destination = tmp_path / name
    shutil.copyfile(fixtures / name, destination)
    return result(destination, **kwargs)


def test_output_plan_preserves_parent_and_handles_long_utf8_name(tmp_path):
    assert plan_output(tmp_path / "movie.mp4") == tmp_path / "movie.embedded.mp4"
    planned = plan_output(tmp_path / (("é" * 115) + ".mkv"))
    assert planned.parent == tmp_path and len(planned.name.encode()) <= 240
    with pytest.raises(OmniError, match="extension"):
        plan_output(tmp_path / "noextension")


def test_missing_requested_metadata_is_a_true_noop():
    downloaded = DownloadResult(Media("https://example.invalid/file", title=""), "missing.mp4", 0, "")
    with patch("omni_dlp.metadata._ffmpeg") as ffmpeg:
        assert embed_metadata(downloaded, embed_tags=False, embed_chapters=True, embed_subtitles=True) is downloaded
        ffmpeg.assert_not_called()


@pytest.mark.parametrize("chapters,duration", [
    ([{"start_time": float("nan"), "end_time": 2}], 2),
    ([{"start_time": "1e999999999", "end_time": 2}], 2),
    ([{"start_time": 0, "end_time": float("inf")}], 2),
    ([{"start_time": -1, "end_time": 1}], 2),
    ([{"start_time": True, "end_time": 1}], 2),
    ([{"start_time": 0, "end_time": 0.0001}], 2),
    ([{"start_time": 1, "end_time": 2}, {"start_time": 0, "end_time": 1}], 2),
    ([{"start_time": 0, "end_time": 1.5}, {"start_time": 1, "end_time": 2}], 2),
    ([{"start_time": 0}], None),
    ([{"start_time": 0, "end_time": 3}], 2),
])
def test_invalid_chapters_are_rejected_before_process_execution(chapters, duration):
    downloaded = DownloadResult(Media("https://example.invalid/file", metadata={"chapters": chapters, "duration": duration}),
                                "missing.mp4", 0, "")
    with patch("omni_dlp.metadata._ffmpeg") as ffmpeg:
        with pytest.raises(OmniError, match="[Cc]hapter"):
            embed_metadata(downloaded, embed_tags=False)
        ffmpeg.assert_not_called()


def test_precancelled_embedding_never_starts_process():
    event = threading.Event()
    event.set()
    downloaded = DownloadResult(Media("https://example.invalid/file"), "missing.mp4", 0, "")
    with patch("omni_dlp.metadata.subprocess.Popen") as process:
        with pytest.raises(OmniError, match="cancelled"):
            embed_metadata(downloaded, cancel_event=event)
        process.assert_not_called()


@pytest.mark.parametrize("extension", ["mp4", "mkv", "mp3", "flac", "ogg", "opus", "wav", "aiff"])
def test_actual_tags_are_embedded_and_source_metadata_is_preserved(fixtures, tmp_path, extension):
    downloaded = source(fixtures, tmp_path, "source." + extension, metadata={"artist": "Fixture artist", "album": "Fixture album",
                        "description": "Descriptive text", "date": "2026-11-08", "uploader": "Fixture uploader"})
    output = embed_metadata(downloaded, embed_chapters=False)
    actual = tags(probe(output.path))
    assert actual["title"] == "New title" and actual["artist"] == "Fixture artist" and actual["album"] == "Fixture album"
    comment_mapping = extension in {"wav", "flac", "ogg", "opus"}
    assert actual["comment"] == ("Descriptive text" if comment_mapping else "Preserved source note")
    assert actual["comment" if comment_mapping else "description"] == "Descriptive text"
    if extension != "wav":
        assert actual["uploader"] == "Fixture uploader"
    else:
        assert output.media.metadata["embedding"]["omitted_tags"] == ["uploader"]
    assert Path(downloaded.path).read_bytes() == (fixtures / ("source." + extension)).read_bytes()
    assert output.path == str(plan_output(downloaded.path))
    assert embed_metadata(downloaded, embed_chapters=False).skipped


@pytest.mark.parametrize("extension", ["mp4", "mkv", "mp3"])
def test_metadata_escaping_chapter_timing_and_injection_resistance(fixtures, tmp_path, extension):
    title = "A = B; # C \\\n[CHAPTER]\nSTART=999999\nEND=9999999"
    chapters = [{"start_time": 0, "title": "Intro = #; \\"}, {"start_time": 1, "title": "Outro\n[STREAM]"}]
    downloaded = source(fixtures, tmp_path, "source." + extension, title=title, metadata={"duration": 2, "chapters": chapters,
                        "artist": "Artist = #; \\ path", "album": "Album\n; comment"})
    output = embed_metadata(downloaded)
    actual = probe(output.path)
    assert tags(actual)["title"] == title
    assert len(actual["chapters"]) == 2
    assert [round(float(chapter["start_time"]), 3) for chapter in actual["chapters"]] == [0, 1]
    assert [round(float(chapter["end_time"]), 3) for chapter in actual["chapters"]] == [1, 2]
    assert actual["chapters"][0]["tags"]["title"] == chapters[0]["title"]
    assert not list(tmp_path.glob(".omni-*"))


@pytest.mark.parametrize("extension", ["mp4", "mkv"])
def test_existing_subtitles_preserved_and_new_language_tagged(fixtures, tmp_path, extension):
    downloaded = source(fixtures, tmp_path, "subbed." + extension)
    subpath = tmp_path / "added.srt"
    shutil.copyfile(fixtures / "added.srt", subpath)
    subtitle = result(subpath, title="English", kind="subtitle", metadata={"language": "en-US"})
    output = embed_metadata(downloaded, subtitles=[subtitle], embed_tags=False, embed_chapters=False, embed_subtitles=True)
    streams = probe(output.path)["streams"]
    subtitles = [stream for stream in streams if stream["codec_type"] == "subtitle"]
    assert [stream["tags"]["language"] for stream in subtitles] == ["deu", "eng"]
    assert [stream["codec_name"] for stream in subtitles] == (["mov_text", "mov_text"] if extension == "mp4" else ["subrip", "subrip"])
    assert {stream["codec_type"] for stream in streams} == {"video", "audio", "subtitle"}
    assert subpath.exists() and Path(downloaded.path).exists()


def test_webvtt_is_embedded_in_matroska(fixtures, tmp_path):
    downloaded = source(fixtures, tmp_path, "source.mkv")
    subpath = tmp_path / "added.vtt"
    shutil.copyfile(fixtures / "added.vtt", subpath)
    subtitle = result(subpath, title="French", kind="subtitle", metadata={"language": "fr"})
    output = embed_metadata(downloaded, subtitles=[subtitle], embed_tags=False, embed_chapters=False, embed_subtitles=True)
    track = next(stream for stream in probe(output.path)["streams"] if stream["codec_type"] == "subtitle")
    assert track["codec_name"] == "webvtt" and track["tags"]["language"] == "fra"


def test_changed_tags_or_subtitle_bytes_invalidate_receipt(fixtures, tmp_path):
    downloaded = source(fixtures, tmp_path, "source.mkv")
    subpath = tmp_path / "added.srt"
    shutil.copyfile(fixtures / "added.srt", subpath)
    subtitle = result(subpath, kind="subtitle", metadata={"language": "es"})
    first = embed_metadata(downloaded, subtitles=[subtitle], embed_subtitles=True)
    with patch("omni_dlp.metadata._execute") as execute:
        assert embed_metadata(downloaded, subtitles=[subtitle], embed_subtitles=True).skipped
        execute.assert_not_called()
    subpath.write_text(subpath.read_text().replace("Added caption", "Changed caption"))
    with pytest.raises(OmniError, match="matching completion record"):
        embed_metadata(downloaded, subtitles=[subtitle], embed_subtitles=True)
    changed = embed_metadata(downloaded, subtitles=[subtitle], embed_subtitles=True, overwrite=True)
    assert changed.sha256 != first.sha256
    downloaded.media.title = "Changed title"
    with pytest.raises(OmniError, match="matching completion record"):
        embed_metadata(downloaded, subtitles=[subtitle], embed_subtitles=True)


def test_new_tags_and_receipts_omit_authentication_material(fixtures, tmp_path):
    downloaded = source(fixtures, tmp_path, "source.mp4", metadata={
        "description": "Listen https://example.invalid/media?token=hidden-url-value\nAuthorization: Bearer hidden-auth-value\npassword=hidden-password-value",
        "Cookie": "hidden-cookie-value", "password": "hidden-field-value", "headers": {"X-Api-Key": "hidden-header-value"}})
    output = embed_metadata(downloaded)
    text = json.dumps(tags(probe(output.path))) + Path(output.path + ".omni.json").read_text()
    for private in ("hidden-url-value", "hidden-auth-value", "hidden-password-value", "hidden-cookie-value",
                    "hidden-field-value", "hidden-header-value", "fixture-private", "secret-fixture"):
        assert private not in text
    assert "https://" not in text


def test_unsupported_containers_subtitle_types_and_symlinks_fail(fixtures, tmp_path):
    downloaded = source(fixtures, tmp_path, "source.flac", metadata={"chapters": [{"start_time": 0, "end_time": 1}]})
    with pytest.raises(OmniError, match="chapter embedding"):
        embed_metadata(downloaded)
    subpath = tmp_path / "caption.xml"
    subpath.write_text("<tt></tt>")
    with pytest.raises(OmniError, match="subtitle embedding"):
        embed_metadata(downloaded, subtitles=[result(subpath)], embed_chapters=False, embed_subtitles=True)
    mp4 = source(fixtures, tmp_path, "source.mp4")
    with pytest.raises(OmniError, match="SRT, WebVTT"):
        embed_metadata(mp4, subtitles=[result(subpath)], embed_subtitles=True)
    link = tmp_path / "linked.mp4"
    try:
        link.symlink_to(mp4.path)
    except OSError:
        pytest.skip("Symlink creation is unavailable")
    with pytest.raises(OmniError, match="symbolic"):
        embed_metadata(replace(mp4, path=str(link)))


def test_playlist_content_disguised_as_local_media_is_not_followed(tmp_path):
    if not shutil.which("ffprobe") or not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg tools are not installed")
    source = tmp_path / "disguised.mp4"
    source.write_text("#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:///private/fixture\n#EXT-X-ENDLIST\n")
    with pytest.raises(OmniError, match="inspect local media"):
        embed_metadata(result(source))
    assert not plan_output(source).exists()


def test_output_limit_and_lock_failures_retain_source(fixtures, tmp_path):
    downloaded = source(fixtures, tmp_path)
    with pytest.raises(OmniError, match="maximum file size"):
        embed_metadata(downloaded, max_filesize=1)
    assert Path(downloaded.path).exists() and not plan_output(downloaded.path).exists()
    with _destination_lock(plan_output(downloaded.path)):
        with pytest.raises(OmniError, match="Another download"):
            embed_metadata(downloaded)
    with _destination_lock(Path(downloaded.path)):
        with pytest.raises(OmniError, match="Another download"):
            embed_metadata(downloaded)
    assert not list(tmp_path.glob(".omni-*"))


def test_real_embedding_cancellation_stops_process_and_removes_temporaries(fixtures, tmp_path):
    downloaded = source(fixtures, tmp_path)
    event, started = threading.Event(), threading.Event()
    failures, processes = [], []
    real_execute, real_popen = operation._execute, subprocess.Popen

    def spawn(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        if Path(args[0][0]).name == "ffmpeg":
            processes.append(process)
            started.set()
        return process

    def slow_execute(_arguments, temporary, limit=None, **kwargs):
        return real_execute(["-re", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "30", "-c:a", "pcm_s16le",
                             "-f", "matroska"], temporary, limit, **kwargs)

    def run():
        try:
            embed_metadata(downloaded, cancel_event=event)
        except BaseException as exc:
            failures.append(exc)

    with patch("omni_dlp.metadata._execute", side_effect=slow_execute), patch("omni_dlp.metadata.subprocess.Popen", side_effect=spawn):
        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            assert started.wait(5)
            time.sleep(0.2)
            assert processes[0].poll() is None
            event.set()
            worker.join(3)
            assert not worker.is_alive() and processes[0].poll() is not None
        finally:
            event.set()
            for process in processes:
                if process.poll() is None:
                    process.kill()
            worker.join(5)
    assert len(failures) == 1 and isinstance(failures[0], OmniError) and "cancelled" in str(failures[0])
    assert Path(downloaded.path).exists() and not plan_output(downloaded.path).exists()
    assert not list(tmp_path.glob(".omni-*"))


@pytest.mark.parametrize("extension", ["mp4", "mkv", "mp3"])
def test_tag_only_update_preserves_existing_chapters_and_all_streams(fixtures, tmp_path, extension):
    downloaded = source(fixtures, tmp_path, "source." + extension, metadata={"duration": 2,
        "chapters": [{"start_time": 0, "end_time": 1, "title": "One"}, {"start_time": 1, "end_time": 2, "title": "Two"}]})
    first = embed_metadata(downloaded)
    before = probe(first.path)
    first.media.title = "Retagged title"
    second = embed_metadata(first, embed_chapters=False)
    after = probe(second.path)
    assert after["chapters"] == before["chapters"]
    assert len(after["streams"]) >= len(before["streams"])
    assert tags(after)["title"] == "Retagged title"
