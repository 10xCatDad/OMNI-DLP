"""Native sidecar and metadata behavior through the actual CLI and archive."""
from dataclasses import replace
import json
import sqlite3
from unittest.mock import patch

import pytest

from omni_dlp.cli import main
from omni_dlp.engine import Downloader

from test_native_engine import item, native_server as native_server
from test_metadata import fixtures as fixtures, probe, tags


def with_assets(base, data):
    data["/caption.vtt"] = b"WEBVTT\n\n00:00:00.000 --> 00:00:01.800\nCaption text\n"
    data["/thumb.jpg"] = b"thumbnail fixture"
    media = item(base)
    media.metadata.update(description="A useful description", subtitles={
        "en": [{"url": base + "/caption.vtt?secret=hidden", "ext": "vtt"}]},
        thumbnails=[{"url": base + "/thumb.jpg?secret=hidden", "width": 1280, "height": 720}])
    return media


def run(media, *arguments):
    # Keep the real registry context, selection, transfers, archive and CLI.
    with patch("omni_dlp.extractors.extract_builtin", return_value=[media]):
        return main([media.source_url or media.url, "--no-plugins", "--retries", "0", *map(str, arguments)])


def test_download_assets_are_byte_verified_and_recorded(native_server, tmp_path, capsys):
    base, data, requests = native_server
    media = with_assets(base, data)
    manifest = tmp_path / "manifest.jsonl"
    assert run(media, "-P", tmp_path, "-o", "%(id)s.%(ext)s", "--write-subs",
               "--write-thumbnail", "--write-description", "--write-info-json", "--manifest", manifest) == 0
    assert (tmp_path / "video.mp4").read_bytes() == data["/high.mp4"]
    assert (tmp_path / "video.en.vtt").read_bytes() == data["/caption.vtt"]
    assert (tmp_path / "video.jpg").read_bytes() == data["/thumb.jpg"]
    assert (tmp_path / "video.description").read_text() == "A useful description"
    record = json.loads(manifest.read_text())
    assert len(record["attachments"]) == 3
    assert all(a["size"] and len(a["sha256"]) == 64 for a in record["attachments"])
    assert "secret=hidden" not in manifest.read_text()
    assert "secret=hidden" not in (tmp_path / "video.mp4.info.json").read_text()
    assert len(requests) == 3
    assert capsys.readouterr().out.count("Downloaded:") == 4


def test_archive_hit_recovers_missing_asset_without_main_transfer(native_server, tmp_path):
    base, data, requests = native_server
    media = with_assets(base, data)
    arguments = ["-P", tmp_path, "-o", "%(id)s.%(ext)s", "--download-archive", tmp_path / "archive.db"]
    assert run(media, *arguments) == 0
    requests.clear()
    assert run(media, *arguments, "--write-subs") == 0
    assert [p.split("?")[0] for p, _ in requests] == ["/caption.vtt"]
    requests.clear()
    assert run(media, *arguments, "--write-subs") == 0
    assert not requests
    (tmp_path / "video.en.vtt").unlink()
    assert run(media, *arguments, "--write-subs") == 0
    assert [p.split("?")[0] for p, _ in requests] == ["/caption.vtt"]


def test_failed_asset_does_not_record_completed_archive_entry(native_server, tmp_path):
    base, data, requests = native_server
    media = with_assets(base, data)
    caption = data.pop("/caption.vtt")
    archive = tmp_path / "archive.db"
    arguments = ["-P", tmp_path, "-o", "%(id)s.%(ext)s", "--write-subs", "--download-archive", archive]
    assert run(media, *arguments) == 1
    assert (tmp_path / "video.mp4").read_bytes() == data["/high.mp4"]
    with sqlite3.connect(archive) as db:
        assert db.execute("SELECT count(*) FROM downloads").fetchone()[0] == 0
    data["/caption.vtt"] = caption
    requests.clear()
    assert run(media, *arguments) == 0
    assert [p.split("?")[0] for p, _ in requests] == ["/caption.vtt"]
    with sqlite3.connect(archive) as db:
        assert db.execute("SELECT count(*) FROM downloads").fetchone()[0] == 1


def test_skip_download_writes_assets_and_explicit_metadata_without_main(native_server, tmp_path):
    base, data, requests = native_server
    media = with_assets(base, data)
    media.formats = []
    media.ext = "mp4"
    media.metadata["formats_required"] = True
    assert run(media, "-P", tmp_path, "-o", "%(id)s.%(ext)s", "--skip-download", "--write-subs",
               "--write-thumbnail", "--write-description", "--write-info-json", "-f", "unavailable") == 0
    assert not (tmp_path / "video.mp4").exists()
    assert not (tmp_path / "video.mp4.omni.json").exists()
    assert set(p.split("?")[0] for p, _ in requests) == {"/caption.vtt", "/thumb.jpg"}
    record = json.loads((tmp_path / "video.mp4.info.json").read_text())
    assert record["downloaded"] is False
    assert "path" not in record and "sha256" not in record


@pytest.mark.parametrize("mode", ["--list-subs", "--list-thumbnails", "--dump-json", "--simulate"])
def test_inspection_modes_never_write_requested_assets(native_server, tmp_path, mode, capsys):
    base, data, requests = native_server
    media = with_assets(base, data)
    target = tmp_path / "unused"
    assert run(media, "-P", target, mode, "--write-subs", "--write-thumbnail", "--write-description") == 0
    output = capsys.readouterr().out
    assert "secret=hidden" not in output
    assert not requests and not target.exists()


@pytest.mark.parametrize("suffix", [".en.vtt", ".en.vtt.omni.json", ".jpg", ".description",
                                    ".mp4.info.json.omni.json", ".en.vtt.info.json.omni.lock"])
def test_manifest_asset_collision_is_rejected_before_any_transfer(native_server, tmp_path, suffix):
    base, data, requests = native_server
    media = with_assets(base, data)
    manifest = tmp_path / ("video" + suffix)
    assert run(media, "-P", tmp_path, "-o", "%(id)s.%(ext)s", "--write-subs",
               "--write-thumbnail", "--write-description", "--manifest", manifest) == 1
    assert not requests and not manifest.exists()


def test_actual_archive_asset_manifest_collision_is_rejected_before_write(native_server, tmp_path):
    base, data, requests = native_server
    media = with_assets(base, data)
    old, new, archive = tmp_path / "old", tmp_path / "new", tmp_path / "archive.db"
    assert run(media, "-P", old, "-o", "%(id)s.%(ext)s", "--download-archive", archive) == 0
    manifest = old / "video.en.vtt"
    manifest.write_bytes(b"existing manifest contents")
    requests.clear()
    assert run(media, "-P", new, "-o", "%(id)s.%(ext)s", "--download-archive", archive,
               "--write-subs", "--manifest", manifest, "--force-overwrites") == 1
    assert manifest.read_bytes() == b"existing manifest contents"
    assert not requests


def test_distinct_media_with_colliding_assets_fail_before_any_transfer(native_server, tmp_path):
    base, data, requests = native_server
    media = with_assets(base, data)
    other = replace(media, url=base + "/other", id="other", ext="mkv", formats=[], title=media.title)
    with patch("omni_dlp.extractors.extract_builtin", return_value=[media, other]):
        assert main([base + "/page", "--no-plugins", "-P", str(tmp_path), "-o", "%(title)s.%(ext)s",
                     "--write-subs"]) == 1
    assert not requests


def test_embedding_runs_after_assets_and_archive_repeat_verifies_same_output(native_server, fixtures, tmp_path):
    base, data, requests = native_server
    media = with_assets(base, data)
    data["/high.mp4"] = (fixtures / "source.mp4").read_bytes()
    media.metadata.update(chapters=[{"start_time": 0, "end_time": 1, "title": "First"},
                                    {"start_time": 1, "end_time": 2, "title": "Second"}], duration=2)
    manifest = tmp_path / "records.jsonl"
    arguments = ["-P", tmp_path, "-o", "%(id)s.%(ext)s", "--embed-metadata", "--embed-chapters",
                 "--embed-subs", "--download-archive", tmp_path / "archive.db", "--manifest", manifest]
    assert run(media, *arguments) == 0
    output = tmp_path / "video.embedded.mp4"
    assert (tmp_path / "video.mp4").read_bytes() == data["/high.mp4"]
    info = probe(output)
    assert tags(info)["title"] == media.title
    assert len(info["chapters"]) == 2
    assert any(s["codec_type"] == "subtitle" and s["tags"]["language"] == "eng" for s in info["streams"])
    record = json.loads(manifest.read_text())
    assert record["path"] == str(output) and len(record["attachments"]) == 1
    requests.clear()
    assert run(media, *arguments) == 0
    assert not requests and not list(tmp_path.glob("*.embedded.embedded.*"))
    repeated = json.loads(manifest.read_text().splitlines()[-1])
    assert repeated["skipped"] and repeated["sha256"] == record["sha256"]
    assert repeated["attachments"][0]["skipped"]


def test_metadata_only_context_is_scoped_to_builtin_and_skips_manifests(native_server):
    from omni_dlp.engine import Options
    base, _, requests = native_server
    media = item(base)
    media.formats[0].protocol = "hls"
    engine = Downloader(Options(metadata_only=True))
    with patch("omni_dlp.engine.discover_plugin", return_value=None) as plugin, \
         patch("omni_dlp.extractors.extract_builtin", return_value=[media]) as builtin:
        engine.inspect(base + "/page")
    assert "metadata_only" not in plugin.call_args.kwargs
    assert builtin.call_args.kwargs["metadata_only"] is True
    assert not requests


def test_skip_download_embedding_conflict_is_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["https://example.invalid/page", "--skip-download", "--embed-subs"])
    assert exc.value.code == 2
    assert "embedding requires" in capsys.readouterr().err


def test_info_json_preserves_unowned_file_and_can_update_owned_record(native_server, tmp_path):
    base, data, _ = native_server
    media = with_assets(base, data)
    info = tmp_path / "video.mp4.info.json"
    info.write_text("User-authored notes")
    arguments = ["-P", tmp_path, "-o", "%(id)s.%(ext)s", "--write-info-json"]
    assert run(media, *arguments) == 1
    assert info.read_text() == "User-authored notes"
    assert run(media, *arguments, "--force-overwrites") == 0
    media.metadata["description"] = "Updated metadata"
    assert run(media, *arguments) == 0
    assert json.loads(info.read_text())["media"]["metadata"]["description"] == "Updated metadata"
    assert json.loads(info.read_text())["skipped"] is True
    info.write_text("Edited by the user")
    assert run(media, *arguments) == 1
    assert info.read_text() == "Edited by the user"
