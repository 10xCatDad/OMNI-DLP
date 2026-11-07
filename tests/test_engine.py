from __future__ import annotations

from dataclasses import replace
import hashlib
import sqlite3
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from omni_dlp.engine import Downloader, Options
from omni_dlp.models import DownloadResult, Media, OmniError
from omni_dlp.transfer import TransferOptions


def test_downloads_are_deduplicated_and_results_preserve_input_order(tmp_path):
    first = Media("https://example.invalid/a.png", title="a", id="1", ext="png", kind="image")
    second = Media("https://example.invalid/b.mp3", title="b", id="2", ext="mp3", kind="audio")
    downloader = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), jobs=2))
    completed = []
    with patch.object(downloader, "fetch_one", side_effect=lambda item: DownloadResult(item, item.id, 1, "abc")) as fetch:
        results = downloader.fetch([first, second, first], on_result=completed.append)
    assert [result.media for result in results] == [first, second]
    assert fetch.call_count == 2 and len(completed) == 2


@pytest.mark.parametrize("items,audio", [
    ([Media("https://example.invalid/a", title="same", ext="png"),
      Media("https://example.invalid/b", title="same", ext="png")], None),
    ([Media("https://example.invalid/a", title="same", ext="mp4"),
      Media("https://example.invalid/b", title="same", ext="webm")], "mp3"),
    ([Media("https://example.invalid/a", title="same", ext="bin", metadata={"protocol": "hls"}),
      Media("https://example.invalid/b", title="same", ext="mkv")], None),
    ([Media("https://example.invalid/a", title="same", ext="png"),
      Media("https://example.invalid/b", title="same", ext="png.omni.json")], None),
])
def test_collisions_are_rejected_before_any_download(tmp_path, items, audio):
    downloader = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path), output="%(title)s.%(ext)s"),
                                    audio_format=audio, jobs=2))
    with patch.object(downloader, "fetch_one") as fetch:
        with pytest.raises(OmniError, match="overlapping"):
            downloader.fetch(items)
        fetch.assert_not_called()


def test_plugin_results_respect_filter_and_limit():
    items = [Media("https://example.invalid/a", kind="image"),
             Media("https://example.invalid/b", kind="audio"),
             Media("https://example.invalid/c", kind="audio")]
    downloader = Downloader(Options(media_types={"audio"}, max_items=1))
    with patch("omni_dlp.engine.discover_plugin", return_value=items), patch("omni_dlp.engine.discover") as native:
        assert downloader.inspect("https://example.invalid/page") == [items[1]]
        native.assert_not_called()


def test_plugin_can_be_disabled():
    downloader = Downloader(Options(plugins=False))
    with patch("omni_dlp.engine.discover_plugin") as plugin, patch("omni_dlp.engine.discover", return_value=[]) as native:
        assert downloader.inspect("https://example.invalid/page") == []
        plugin.assert_not_called()
        native.assert_called_once()


def test_plugin_entry_point_contract_and_failure_sanitization():
    from omni_dlp.plugins import discover_plugin
    media = Media("https://example.invalid/file.png", kind="image")
    plugin = SimpleNamespace(supports=lambda url: True, discover=lambda url, **kwargs: [media])
    entry = SimpleNamespace(name="fixture", load=lambda: plugin)
    with patch("omni_dlp.plugins.entry_points", return_value=[entry]):
        assert discover_plugin("https://example.invalid/page") == [media]
    plugin.discover = lambda *args, **kwargs: ["invalid result with private data"]
    with patch("omni_dlp.plugins.entry_points", return_value=[entry]):
        with pytest.raises(OmniError, match="fixture.*TypeError") as error:
            discover_plugin("https://example.invalid/page")
        assert "private data" not in str(error.value)


def test_conversion_preserves_output_metadata(tmp_path):
    media = Media("https://example.invalid/video.mp4", kind="video", ext="mp4")
    converted = replace(media, kind="audio", ext="mp3", metadata={"conversion": "mp3"})
    raw = DownloadResult(media, str(tmp_path / "video.mp4"), 5, "abc")
    result = DownloadResult(converted, str(tmp_path / "video.mp3"), 3, "def")
    downloader = Downloader(Options(audio_format="mp3"))
    with patch("omni_dlp.engine.download", return_value=raw), patch("omni_dlp.streams.transcode", return_value=result):
        actual = downloader.fetch_one(media)
    assert actual.media.ext == "mp3" and actual.media.kind == "audio"
    assert actual.media.url == media.url


def test_archive_roundtrip_preserves_converted_output_metadata(tmp_path):
    media = Media("https://example.invalid/video.mp4?secret=not-stored", kind="video", ext="mp4")
    converted = replace(media, kind="audio", ext="mp3", mime_type="", metadata={"conversion": "mp3"})
    path = tmp_path / "video.mp3"
    content = b"converted audio"
    path.write_bytes(content)
    raw = DownloadResult(media, str(tmp_path / "video.mp4"), 5, "abc")
    result = DownloadResult(converted, str(path), len(content), hashlib.sha256(content).hexdigest())
    downloader = Downloader(Options(audio_format="mp3", archive=str(tmp_path / "archive.db"),
                                    transfer=TransferOptions(directory=str(tmp_path))))
    with patch("omni_dlp.engine.download", return_value=raw) as download, patch("omni_dlp.streams.transcode", return_value=result):
        assert not downloader.fetch_one(media).skipped
        actual = downloader.fetch_one(media)
    assert download.call_count == 1
    assert actual.skipped and actual.media.ext == "mp3" and actual.media.kind == "audio"
    assert actual.media.metadata["conversion"] == "mp3"
    assert b"secret=not-stored" not in (tmp_path / "archive.db").read_bytes()


def test_archive_existing_schema_is_migrated(tmp_path):
    from omni_dlp.archive import Archive
    archive = tmp_path / "old.db"
    with sqlite3.connect(archive) as db:
        db.execute("CREATE TABLE downloads(key TEXT PRIMARY KEY, path TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL)")
    Archive(str(archive))
    with sqlite3.connect(archive) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(downloads)")}
    assert {"output_kind", "output_ext", "output_mime"}.issubset(columns)


def test_error_callback_allows_other_items_to_complete(tmp_path):
    good, bad = Media("https://example.invalid/a", id="a"), Media("https://example.invalid/b", id="b")
    downloader = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path)), jobs=2))
    errors = []

    def fetch(item):
        if item is bad:
            raise OmniError("fixture failure")
        return DownloadResult(item, "a", 1, "abc")

    with patch.object(downloader, "fetch_one", side_effect=fetch):
        results = downloader.fetch([good, bad], on_error=lambda item, exc: errors.append((item, exc)))
    assert len(results) == 1 and results[0].media is good
    assert len(errors) == 1 and errors[0][0] is bad


def test_invalid_archive_is_actionable(tmp_path):
    archive = tmp_path / "broken.db"
    archive.write_text("not a SQLite database")
    with pytest.raises(OmniError, match="SQLite"):
        Downloader(Options(archive=str(archive)))


@pytest.mark.parametrize("suffix", ["", "-wal", "-shm", "-journal"])
def test_outputs_cannot_replace_archive_or_sqlite_journal(tmp_path, suffix):
    archive = tmp_path / "archive.db"
    downloader = Downloader(Options(archive=str(archive), transfer=TransferOptions(
        directory=str(tmp_path), output="archive.db" + suffix, overwrite=True)))
    before = archive.read_bytes()
    with patch("omni_dlp.engine.download") as download:
        with pytest.raises(OmniError, match="overlaps the download archive"):
            downloader.fetch_one(Media("https://example.invalid/a.png"))
        download.assert_not_called()
    assert archive.read_bytes() == before


@pytest.mark.parametrize("options", [Options(jobs=True), Options(jobs=0), Options(max_items=0),
    Options(transfer=TransferOptions(timeout=float("nan"))), Options(audio_format="mp3", remux_format="mp4")])
def test_invalid_options_fail_before_work(options):
    with pytest.raises(OmniError):
        Downloader(options)


def test_interrupt_signals_workers_before_waiting_and_restores_options(tmp_path):
    started, stopped = Event(), Event()
    downloader = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path))))

    def fetch(item):
        event = downloader.options.transfer.cancel_event
        started.set()
        assert event.wait(3), "worker did not receive cancellation"
        stopped.set()
        raise OmniError("Download cancelled")

    def interrupted(_):
        assert started.wait(3)
        raise KeyboardInterrupt

    with patch.object(downloader, "fetch_one", side_effect=fetch), patch("omni_dlp.engine.as_completed", side_effect=interrupted):
        with pytest.raises(KeyboardInterrupt):
            downloader.fetch([Media("https://example.invalid/a", id="a")])
    assert stopped.is_set()
    assert downloader.options.transfer.cancel_event is None


def test_direct_mode_still_filters_media_types():
    downloader = Downloader(Options(direct=True, media_types={"image"}))
    assert downloader.inspect("https://example.invalid/a.json") == []
    assert downloader.inspect("https://example.invalid/a.png")[0].kind == "image"
