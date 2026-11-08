"""Composable discovery and concurrent downloads, independent of CLI parsing."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, replace
import math
from pathlib import Path
import sqlite3
from threading import Event
from typing import TYPE_CHECKING, Callable

from .archive import Archive
from .discovery import discover
from .models import DownloadResult, Format, Media, OmniError
from .plugins import discover_plugin
from .transfer import OUTPUT_SIDECARS, TransferOptions, download

if TYPE_CHECKING:
    from .assets import AssetOptions
    from .collection_selection import CollectionOptions


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
    direct: bool = False
    format: str | None = None
    format_sort: str | None = None
    merge_output_format: str = "mkv"
    collection: CollectionOptions | None = None
    assets: AssetOptions | None = None
    embed_tags: bool = False
    embed_chapters: bool = False
    embed_subtitles: bool = False
    reserved_paths: list[str] = field(default_factory=list)
    metadata_only: bool = False


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
        if opts.direct:
            import hashlib
            from urllib.parse import unquote, urldefrag, urlsplit
            from .transfer import validate_url
            from .types import classify
            validate_url(url)
            url = urldefrag(url)[0]
            kind, extension = classify(url)
            if opts.media_types and kind not in opts.media_types:
                return []
            basename = Path(unquote(urlsplit(url).path)).name
            result = [Media(url=url, title=Path(basename).stem or "media",
                          id=hashlib.sha256(url.encode()).hexdigest()[:16], kind=kind,
                          ext=extension, source_url=url)]
            if opts.collection is not None:
                from .collection_selection import select_entries
                result = select_entries(result, opts.collection)
            return result
        kwargs = dict(headers=opts.transfer.headers, cookies=opts.transfer.cookies,
                      timeout=opts.transfer.timeout, media_types=None if opts.collection is not None else opts.media_types,
                      max_items=opts.max_items)
        result = discover_plugin(url, **kwargs) if opts.plugins else None
        if result is None:
            from .extractors import extract_builtin
            # Built-ins use media_types to expose optional artwork/captions and
            # retain source indices themselves. Generic discovery and plugins
            # are enumerated before the engine applies its kind filter.
            result = extract_builtin(url, **{**kwargs, "media_types": opts.media_types}, metadata_only=opts.metadata_only)
        if result is None:
            result = discover(url, **kwargs)
        # Preserve the original enumeration before media-kind filtering and
        # local bounds. A loaded prefix is not a complete playlist.
        discovered = len(result)
        if opts.collection is not None:
            from .collection_selection import select_entries
            has_count = any(item.metadata.get("playlist_count") is not None for item in result)
            reports_complete = any(item.metadata.get("collection_complete") is True for item in result)
            if discovered == opts.max_items and not has_count and not reports_complete:
                result = [replace(item, metadata={**item.metadata, "discovery_limited": True}) for item in result]
            # Normalize the whole source together. This validates conflicting
            # counts before filtering and propagates a reported count without
            # substituting the loaded length for an unknown collection total.
            result = select_entries(result)
        if opts.media_types:
            result = [item for item in result if item.kind in opts.media_types]
            if len(result) != discovered and opts.collection is not None:
                result = [replace(item, metadata={**item.metadata,
                    "collection_complete": False}) for item in result]
        if len(result) > opts.max_items and opts.collection is not None:
            result = [replace(item, metadata={**item.metadata, "collection_complete": False,
                "collection_truncated": True}) for item in result[:opts.max_items]]
        result = result[:opts.max_items]
        if opts.collection is not None:
            result = select_entries(result, opts.collection)
        return result if opts.metadata_only else self._expand_manifests(result, url)

    def _expand_manifests(self, media: list[Media], source: str) -> list[Media]:
        from .extractors.common import ExtractorContext
        from .manifests import parse_hls
        context = ExtractorContext(source, headers=self.options.transfer.headers,
                                   cookies=self.options.transfer.cookies, timeout=self.options.transfer.timeout)
        cache = {}
        requests = 0
        output = []
        for item in media:
            formats = []
            warnings = []
            for fmt in item.formats:
                if fmt.protocol not in {"hls", "m3u8", "m3u8_native"}:
                    formats.append(fmt)
                    continue
                cache_key = (fmt.url, tuple(sorted(fmt.headers.items())))
                if cache_key not in cache and requests >= 16:
                    formats.append(fmt)
                    warnings.append("Manifest expansion request limit reached")
                    continue
                try:
                    if cache_key not in cache:
                        requests += 1
                        data, final_url, final_headers = context.get_document(fmt.url, headers=fmt.headers)
                        text = data.decode("utf-8-sig", errors="replace")
                        cache[cache_key] = parse_hls(text, final_url, headers=final_headers)
                    candidates = cache[cache_key]
                    if candidates:
                        formats.extend(replace(f, format_id=f"{fmt.format_id}-{f.format_id}",
                            metadata={**fmt.metadata, **f.metadata}) for f in candidates)
                    else:
                        formats.append(fmt)
                except OmniError as exc:
                    # Keep alternate progressive representations usable. Failed
                    # manifests are not advertised as verified representations.
                    warnings.append(str(exc))
            metadata = dict(item.metadata)
            if item.formats:
                metadata["formats_required"] = True
            if warnings:
                metadata["format_warnings"] = warnings
            output.append(replace(item, formats=formats, metadata=metadata))
        return output

    def prepare(self, media: list[Media], *,
                on_error: Callable[[Media, Exception], None] | None = None) -> list[Media]:
        """Select representations, optionally continuing after an item fails."""
        prepared = []
        for item in media:
            try:
                prepared.extend(self._prepare([item]))
            except OmniError as exc:
                if on_error is None:
                    raise
                on_error(item, exc)
        return prepared

    def _prepare(self, media: list[Media]) -> list[Media]:
        """Select concrete representations without mutating extractor records."""
        from .selection import select_formats
        from .transfer import _origin
        selected = []
        for item in media:
            if not item.formats and item.metadata.get("formats_required"):
                raise OmniError("No playable format candidates remain after manifest inspection")
            if item.requested_formats or not item.formats:
                if self.options.format and not item.requested_formats:
                    candidates = [Format(item.url, format_id="direct", ext=item.ext,
                                         vcodec="none" if item.kind == "audio" else "",
                                         acodec="none" if item.kind not in {"audio", "video"} else "")]
                else:
                    if item.metadata.get("archive_id") and not item.metadata.get("resource_id"):
                        item = replace(item, metadata={**item.metadata, "resource_id": str(item.metadata["archive_id"])})
                    selected.append(item)
                    continue
            else:
                candidates = item.formats
            expression = self.options.format
            if not expression:
                expression = "bestaudio/best" if item.kind == "audio" else "bestvideo+bestaudio/best"
                if item.kind not in {"audio", "video"}:
                    expression = "best*"
            groups = select_formats(candidates, expression, self.options.format_sort)
            if not groups:
                raise OmniError(f"No formats match the requested selection for item {item.id or 'unknown'!r}")
            for group in groups:
                if not group:
                    continue
                combined = "+".join(fmt.format_id or "unknown" for fmt in group)
                metadata = {**item.metadata, "format_id": combined}
                logical = item.metadata.get("archive_id")
                if logical:
                    metadata["resource_id"] = str(logical) + "|format:" + combined
                identity = item.id + "." + combined if len(groups) > 1 else item.id
                if len(group) > 1:
                    selected.append(replace(item, id=identity, ext=self.options.merge_output_format,
                                            metadata=metadata, requested_formats=group))
                else:
                    fmt = group[0]
                    inherited = item.headers
                    if _origin(item.url) != _origin(fmt.url):
                        inherited = {k: v for k, v in inherited.items() if k.lower() in {
                            "user-agent", "accept", "accept-language", "accept-encoding"}}
                    reserved = {key: metadata[key] for key in (
                        "resource_id", "archive_id", "format_id", "extractor", "formats_required") if key in metadata}
                    metadata = {**metadata, **fmt.metadata, **reserved, "protocol": fmt.protocol}
                    kind = "audio" if fmt.vcodec == "none" and fmt.acodec != "none" else item.kind
                    selected.append(replace(item, url=fmt.url, id=identity, kind=kind,
                        ext=fmt.ext or item.ext, mime_type="", metadata=metadata,
                        source_url=item.source_url or item.url, headers={**inherited, **fmt.headers}, requested_formats=[fmt]))
        return selected

    def fetch_one(self, media: Media) -> DownloadResult:
        opts = self.options
        if not media.requested_formats:
            prepared = self.prepare([media])
            if len(prepared) != 1:
                raise OmniError("Selection produces multiple outputs; use Downloader.fetch")
            media = prepared[0]
        if opts.transfer.cancel_event is not None and opts.transfer.cancel_event.is_set():
            raise OmniError("Download cancelled")
        # Protect archive storage even for callers using this single-item API.
        self.output_paths(media)
        variant = self._archive_variant(media)
        existing = self._archive_lookup(media)
        if existing:
            return self._finish(existing)
        protocol = media.metadata.get("protocol")
        if len(media.requested_formats) > 1:
            from .merge import download_merged
            result = download_merged(media, opts.transfer, container=opts.merge_output_format)
        elif not opts.direct and (protocol in {"hls", "dash", "m3u8", "m3u8_native"} or media.ext in {"m3u8", "mpd"}):
            from .streams import download_stream
            result = download_stream(media, opts.transfer)
        else:
            result = download(media, opts.transfer)
        if opts.audio_format or opts.remux_format:
            from .streams import transcode
            result = transcode(result, audio_format=opts.audio_format, remux_format=opts.remux_format,
                               overwrite=opts.transfer.overwrite, cancel_event=opts.transfer.cancel_event)
        finished = self._finish(result)
        if self.archive:
            try:
                self.archive.record(result, variant)
            except sqlite3.Error as exc:
                raise OmniError("Cannot update the SQLite download archive") from exc
        return finished

    def _archive_variant(self, media: Media) -> str:
        opts = self.options
        planned = self._base_output_paths(media)
        variant = f"audio={opts.audio_format};remux={opts.remux_format}"
        if len(media.requested_formats) > 1:
            variant += ";merge=" + (planned[-1].suffix.lower().lstrip(".") or opts.merge_output_format)
        elif not opts.direct and (media.ext in {"m3u8", "mpd"} or media.metadata.get("protocol") in {"hls", "dash", "m3u8", "m3u8_native"}):
            variant += ";adaptive=" + (planned[0].suffix.lower().lstrip(".") or "mkv")
        if opts.direct:
            variant += ";direct=True"
        return variant

    def _archive_lookup(self, media: Media) -> DownloadResult | None:
        opts = self.options
        if self.archive:
            try:
                existing = self.archive.lookup(media, self._archive_variant(media))
            except sqlite3.Error as exc:
                raise OmniError("Cannot read the SQLite download archive") from exc
            if existing:
                if opts.audio_format or opts.remux_format:
                    existing.media = replace(existing.media, metadata={**existing.media.metadata,
                        "conversion": opts.audio_format or opts.remux_format})
                return existing
        return None

    def _asset_plans(self, item: Media, main_path: Path):
        if self.options.assets is None and not self.options.embed_subtitles:
            return []
        from .assets import AssetOptions, plan_assets
        options = self.options.assets or AssetOptions()
        if self.options.embed_subtitles and not (options.write_subs or options.write_auto_subs):
            options = replace(options, write_subs=True)
        return plan_assets(item, main_path, options)

    def _embedding(self) -> bool:
        return self.options.embed_tags or self.options.embed_chapters or self.options.embed_subtitles

    def _check_paths(self, paths: list[Path]) -> None:
        protected = {str(Path(path).expanduser().resolve()).casefold() for path in self.options.reserved_paths}
        if self.options.archive:
            archive = str(Path(self.options.archive).expanduser().resolve()).casefold()
            protected.update(archive + suffix for suffix in ("", "-wal", "-shm", "-journal"))
        seen = set()
        for path in paths:
            related = {str(Path(str(path) + suffix).resolve()).casefold() for suffix in OUTPUT_SIDECARS}
            if related.intersection(protected):
                raise OmniError("A planned or actual media output or sidecar overlaps the download archive or a reserved path")
            if related.intersection(seen):
                raise OmniError("Media and auxiliary outputs overlap; choose distinct output names")
            seen.update(related)

    def _result_paths(self, result: DownloadResult) -> list[Path]:
        plans = self._asset_plans(result.media, Path(result.path))
        paths = [Path(result.path), *(plan.path for plan in plans)]
        if self._embedding():
            from .metadata import plan_output
            paths.append(plan_output(Path(result.path)))
        # Archive hits can point to a previous directory; validate actual paths
        # before writing anything beside that file.
        self._check_paths(paths)
        return paths

    def _finish(self, result: DownloadResult) -> DownloadResult:
        self._result_paths(result)
        plans = self._asset_plans(result.media, Path(result.path))
        attachments = []
        if plans:
            from .assets import download_assets
            # A verified archive hit authorizes its own containing directory.
            # Every asset remains confined beside that file, even if today's
            # configured download directory differs.
            transfer = replace(self.options.transfer, directory=str(Path(result.path).resolve().parent))
            attachments = download_assets(plans, transfer)
        if self._embedding():
            from .metadata import embed_metadata
            result = embed_metadata(result, subtitles=[a for a in attachments if a.media.kind == "subtitle"],
                embed_tags=self.options.embed_tags, embed_chapters=self.options.embed_chapters,
                embed_subtitles=self.options.embed_subtitles, overwrite=self.options.transfer.overwrite,
                cancel_event=self.options.transfer.cancel_event, max_filesize=self.options.transfer.max_filesize)
        return replace(result, attachments=attachments)

    def asset_paths(self, item: Media) -> list[Path]:
        base = self._base_output_paths(item)[-1]
        return [plan.path for plan in self._asset_plans(item, base)]

    def fetch_assets(self, media: list[Media], *, on_result=None, on_error=None) -> list[DownloadResult]:
        """Download requested sidecars without requiring playable main formats."""
        from .assets import download_assets
        all_paths = []
        planned = []
        for item in {item.url: item for item in media}.values():
            try:
                base = self._base_output_paths(item)[-1]
                plans = self._asset_plans(item, base)
                all_paths.extend([base, *(plan.path for plan in plans)])
                planned.append((item, plans))
            except OmniError as exc:
                if on_error is None:
                    raise
                on_error(item, exc)
        self._check_paths(all_paths)
        completed = []
        for item, plans in planned:
            try:
                results = download_assets(plans, self.options.transfer)
                completed.extend(results)
                if on_result:
                    for result in results:
                        on_result(result)
            except (OmniError, OSError) as exc:
                if on_error is None:
                    raise
                on_error(item, exc)
        return completed

    def output_paths(self, item: Media) -> list[Path]:
        destinations = self._base_output_paths(item)
        base = destinations[-1]
        destinations.extend(plan.path for plan in self._asset_plans(item, base))
        if self._embedding():
            from .metadata import plan_output
            destinations.append(plan_output(base))
        self._check_paths(destinations)
        return destinations

    def _base_output_paths(self, item: Media) -> list[Path]:
        """Planned media outputs, including retained conversion inputs."""
        from .transfer import output_path
        adaptive = not self.options.direct and (item.ext in {"m3u8", "mpd"} or item.metadata.get("protocol") in {"hls", "dash", "m3u8", "m3u8_native"})
        if len(item.requested_formats) > 1:
            from .merge import planned_paths
            destinations = planned_paths(item, self.options.transfer, container=self.options.merge_output_format)
            target = destinations[-1]
        else:
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
                related = {str(destination).casefold() + suffix for suffix in OUTPUT_SIDECARS}
                if related.intersection(protected):
                    raise OmniError("A media output or sidecar overlaps the download archive")
        return destinations

    def fetch(self, media: list[Media], *,
              on_result: Callable[[DownloadResult], None] | None = None,
              on_error: Callable[[Media, Exception], None] | None = None) -> list[DownloadResult]:
        # Deduplicate before submitting; avoid two writers racing for the same URL.
        def key(item):
            return item.url, tuple(fmt.format_id for fmt in item.requested_formats)
        unique = list({key(item): item for item in self.prepare(media, on_error=on_error)}.values())
        paths: set[str] = set()
        for item in unique:
            occupied = set()
            destinations = self.output_paths(item)
            existing = self._archive_lookup(item)
            if existing:
                destinations.extend(self._result_paths(existing))
            for destination in destinations:
                for suffix in OUTPUT_SIDECARS:
                    occupied.add(str(destination).casefold() + suffix)
            if paths.intersection(occupied):
                raise OmniError("Multiple items resolve to overlapping output paths; include %(id)s in --output")
            paths.update(occupied)
        completed: dict[tuple, DownloadResult] = {}
        transfer = self.options.transfer
        previous_event = transfer.cancel_event
        cancel_event = previous_event if previous_event is not None else Event()
        transfer.cancel_event = cancel_event
        pool = ThreadPoolExecutor(max_workers=self.options.jobs)
        pending = {}
        try:
            pending = {pool.submit(self.fetch_one, item): item for item in unique}
            for future in as_completed(pending):
                item = pending[future]
                try:
                    result = future.result()
                except Exception as exc:
                    if on_error:
                        on_error(item, exc)
                    else:
                        raise
                else:
                    completed[key(item)] = result
                    if on_result:
                        on_result(result)
        except BaseException:
            # Set cancellation before waiting for workers: a context manager
            # alone would wait for a live/large download to finish after Ctrl-C.
            cancel_event.set()
            for job in pending:
                job.cancel()
            raise
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            transfer.cancel_event = previous_event
        return [completed[key(item)] for item in unique if key(item) in completed]
