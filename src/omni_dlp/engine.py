"""Composable discovery and concurrent downloads, independent of CLI parsing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import sqlite3
from typing import Callable

from .archive import Archive
from .discovery import discover
from .models import DownloadResult, Media, OmniError
from .plugins import discover_plugin
from .transfer import TransferOptions, download


@dataclass
class Options:
    transfer: TransferOptions = field(default_factory=TransferOptions)
    media_types: set[str] | None = None
    jobs: int = 1
    max_items: int = 1000
    archive: str | None = None
    audio_format: str | None = None
    remux_format: str | None = None
    plugins: bool = True


class Downloader:
    def __init__(self, options: Options | None = None):
        self.options = options or Options()
        if type(self.options.jobs) is not int or not 1 <= self.options.jobs <= 64:
            raise OmniError("jobs must be between 1 and 64")
        if type(self.options.max_items) is not int or self.options.max_items < 1:
            raise OmniError("max_items must be positive")
        if self.options.audio_format and self.options.remux_format:
            raise OmniError("Choose either audio conversion or remuxing")
        timeout = self.options.transfer.timeout
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise OmniError("timeout must be finite and positive")
        try:
            self.archive = Archive(self.options.archive) if self.options.archive else None
        except sqlite3.Error as exc:
            raise OmniError("Cannot initialize the SQLite download archive") from exc

    def inspect(self, url: str) -> list[Media]:
        opts = self.options
        kwargs = dict(headers=opts.transfer.headers, cookies=opts.transfer.cookies,
                      timeout=opts.transfer.timeout, media_types=opts.media_types,
                      max_items=opts.max_items)
        result = discover_plugin(url, **kwargs) if opts.plugins else None
        if result is None:
            return discover(url, **kwargs)
        if opts.media_types:
            result = [item for item in result if item.kind in opts.media_types]
        return result[:opts.max_items]

    def fetch_one(self, media: Media) -> DownloadResult:
        opts = self.options
        # Protect archive storage even for callers using this single-item API.
        self.output_paths(media)
        variant = f"audio={opts.audio_format};remux={opts.remux_format}"
        if self.archive:
            try:
                existing = self.archive.lookup(media, variant)
            except sqlite3.Error as exc:
                raise OmniError("Cannot read the SQLite download archive") from exc
            if existing:
                if opts.audio_format or opts.remux_format:
                    existing.media = replace(existing.media, metadata={**existing.media.metadata,
                        "conversion": opts.audio_format or opts.remux_format})
                return existing
        protocol = media.metadata.get("protocol")
        if protocol in {"hls", "dash"} or media.ext in {"m3u8", "mpd"}:
            from .streams import download_stream
            result = download_stream(media, opts.transfer)
        else:
            result = download(media, opts.transfer)
        if opts.audio_format or opts.remux_format:
            from .streams import transcode
            result = transcode(result, audio_format=opts.audio_format, remux_format=opts.remux_format,
                               overwrite=opts.transfer.overwrite)
        if self.archive:
            try:
                self.archive.record(result, variant)
            except sqlite3.Error as exc:
                raise OmniError("Cannot update the SQLite download archive") from exc
        return result

    def output_paths(self, item: Media) -> list[Path]:
        """Planned media outputs, including retained conversion inputs."""
        from .transfer import output_path
        adaptive = item.ext in {"m3u8", "mpd"} or item.metadata.get("protocol") in {"hls", "dash"}
        mapped = replace(item, ext="mkv") if adaptive else item
        target = output_path(mapped, self.options.transfer)
        destinations = [target]
        extension = self.options.audio_format or self.options.remux_format
        if extension:
            converted = target.with_suffix("." + extension)
            if converted == target:
                converted = target.with_name(target.stem + ".converted." + extension)
            destinations.append(converted)
        if self.options.archive:
            archive = str(Path(self.options.archive).expanduser().resolve()).casefold()
            protected = {archive + suffix for suffix in ("", "-wal", "-shm", "-journal")}
            for destination in destinations:
                related = {str(destination).casefold() + suffix for suffix in (
                    "", ".part", ".part.json", ".omni.json", ".omni.lock", ".info.json")}
                if related.intersection(protected):
                    raise OmniError("A media output or sidecar overlaps the download archive")
        return destinations

    def fetch(self, media: list[Media], *,
              on_result: Callable[[DownloadResult], None] | None = None,
              on_error: Callable[[Media, Exception], None] | None = None) -> list[DownloadResult]:
        # Deduplicate before submitting; avoid two writers racing for the same URL.
        unique = list({item.url: item for item in media}.values())
        paths: set[str] = set()
        for item in unique:
            occupied = set()
            for destination in self.output_paths(item):
                for suffix in ("", ".part", ".part.json", ".omni.json", ".omni.lock", ".info.json"):
                    occupied.add(str(destination).casefold() + suffix)
            if paths.intersection(occupied):
                raise OmniError("Multiple items resolve to overlapping output paths; include %(id)s in --output")
            paths.update(occupied)
        completed: dict[str, DownloadResult] = {}
        with ThreadPoolExecutor(max_workers=self.options.jobs) as pool:
            pending = {pool.submit(self.fetch_one, item): item for item in unique}
            for future in as_completed(pending):
                item = pending[future]
                try:
                    result = future.result()
                except Exception as exc:
                    if on_error:
                        on_error(item, exc)
                    else:
                        for job in pending:
                            job.cancel()
                        raise
                else:
                    completed[item.url] = result
                    if on_result:
                        on_result(result)
        return [completed[item.url] for item in unique if item.url in completed]
