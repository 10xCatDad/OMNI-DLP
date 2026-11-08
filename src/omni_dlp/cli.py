"""Native CLI plus a deliberately separate upstream-compatibility command."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import sys
import tomllib
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .models import OmniError


def size(value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*([KMGT]?(?:i?B)?)?", value, re.I)
    if not match:
        raise argparse.ArgumentTypeError("expected bytes or a size such as 10M or 1GiB")
    suffix = (match[2] or "").upper().replace("IB", "").replace("B", "")
    try:
        number = int(float(match[1]) * 1024 ** ("KMGT".index(suffix) + 1 if suffix else 0))
    except (OverflowError, ValueError) as exc:
        raise argparse.ArgumentTypeError("size is too large") from exc
    if number < 1:
        raise argparse.ArgumentTypeError("size must be positive")
    return number


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="omni-dlp", description="Download images, audio, video and other files.",
        epilog="Use 'omni-dlp compat [yt-dlp options]' for upstream site extractors and options.")
    p.add_argument("urls", nargs="*", metavar="URL")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", help="Explicit TOML file containing an [omni] table")
    p.add_argument("-a", "--batch-file", help="Read URLs, one per line; '-' reads stdin")
    p.add_argument("-P", "--paths", dest="directory", default=".", help="Output directory")
    p.add_argument("-o", "--output", default="%(title)s [%(id)s].%(ext)s")
    p.add_argument("--media-type", action="append", choices=["image", "audio", "video", "document", "archive", "font", "model", "subtitle", "other"], help="Include only this kind (repeatable)")
    p.add_argument("--jobs", type=int, default=1, help="Concurrent downloads, 1 to 64")
    p.add_argument("--max-items", type=int, default=1000, help="Maximum discovered items per URL")
    p.add_argument("-I", "--playlist-items", help="Source item indices, inclusive ranges, or slices")
    p.add_argument("--playlist-start", type=int, default=1)
    p.add_argument("--playlist-end", type=int)
    p.add_argument("--playlist-reverse", action="store_true")
    p.add_argument("--playlist-random", action="store_true")
    p.add_argument("--playlist-random-seed", type=int, help="Reproducible collection shuffle")
    p.add_argument("--date", help="Only entries uploaded on this date (YYYYMMDD or ISO date)")
    p.add_argument("--datebefore", help="Include entries on or before this date")
    p.add_argument("--dateafter", help="Include entries on or after this date")
    p.add_argument("--match-filter", help="Native scalar metadata filter; combine conditions with &")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--socket-timeout", dest="timeout", type=float, default=30)
    p.add_argument("-r", "--limit-rate", type=size)
    p.add_argument("--max-filesize", type=size)
    p.add_argument("--no-continue", dest="resume", action="store_false", default=True)
    p.add_argument("--force-overwrites", action="store_true")
    p.add_argument("--add-header", action="append", default=[], metavar="NAME:VALUE")
    p.add_argument("--user-agent")
    p.add_argument("--referer")
    p.add_argument("--cookies", help="Netscape cookie file")
    p.add_argument("--download-archive", help="SQLite archive; existing output is verified before skipping")
    p.add_argument("-s", "--simulate", action="store_true")
    p.add_argument("-j", "--dump-json", action="store_true", help="Print discovered metadata as JSON lines, without downloading")
    p.add_argument("--write-info-json", action="store_true", help="Write credential-redacted metadata beside downloads")
    p.add_argument("--skip-download", action="store_true", help="Write requested sidecars without downloading the main media")
    p.add_argument("--write-subs", action="store_true")
    p.add_argument("--write-auto-subs", action="store_true")
    p.add_argument("--sub-langs", help="Comma-separated language patterns; all and -exclusions supported")
    p.add_argument("--sub-format", default="vtt/srt/best", help="Subtitle format fallback order")
    p.add_argument("--list-subs", action="store_true")
    p.add_argument("--write-thumbnail", action="store_true")
    p.add_argument("--write-all-thumbnails", action="store_true")
    p.add_argument("--list-thumbnails", action="store_true")
    p.add_argument("--write-description", action="store_true")
    p.add_argument("--embed-metadata", action="store_true", help="Write supported tags to a retained .embedded media copy")
    p.add_argument("--embed-chapters", action="store_true", help="Write chapters to a retained .embedded media copy")
    p.add_argument("--embed-subs", action="store_true", help="Embed selected subtitles into a retained .embedded media copy")
    p.add_argument("--manifest", help="Append completed download records as flushed JSON lines")
    p.add_argument("--progress-json", action="store_true", help="Emit flushed JSON completion/error events on stderr")
    p.add_argument("-f", "--format", help="Native format selector: IDs, filters, / fallback, + merge, , multiple outputs")
    p.add_argument("-S", "--format-sort", help="Comma-separated native format sort keys")
    p.add_argument("-F", "--list-formats", action="store_true", help="List available representations without downloading")
    p.add_argument("--list-extractors", action="store_true", help="List native built-in extractor coverage")
    p.add_argument("--merge-output-format", choices=["mkv", "mp4", "webm", "mov"], default="mkv")
    p.add_argument("-x", "--extract-audio", action="store_true")
    p.add_argument("--audio-format", choices=["mp3", "m4a", "m4b", "aac", "flac", "opus", "ogg", "wav", "aiff"], default="mp3")
    p.add_argument("--remux-video", choices=["mp4", "mkv", "webm", "mov"])
    p.add_argument("--no-plugins", action="store_true")
    p.add_argument("--direct", action="store_true", help="Download each URL as raw bytes, without discovery or plugins")
    p.add_argument("--ignore-errors", action="store_true", help="Continue remaining URLs; exit status remains nonzero on failures")
    p.add_argument("-q", "--quiet", action="store_true")
    return p


def display_text(value) -> str:
    """Keep untrusted site metadata from emitting terminal control sequences."""
    return "".join(char if char.isprintable() else " " for char in str(value))


def redact_url(value: str) -> str:
    try:
        u = urlsplit(value)
        if u.scheme not in {"http", "https"}:
            return value
        hostname = u.hostname or ""
        if ":" in hostname:
            hostname = f"[{hostname}]"
        authority = hostname + (f":{u.port}" if u.port else "")
        return urlunsplit((u.scheme, authority, u.path, "", ""))
    except ValueError:
        return "[invalid URL]"


def public_record(item):
    """Sidecars do not persist headers, URL credentials or signed query parameters."""
    if isinstance(item, dict):
        return {key: public_record(value) for key, value in item.items()
                if key.lower() not in {"headers", "http_headers", "cookies", "authorization", "password", "token"}}
    if isinstance(item, list):
        return [public_record(value) for value in item]
    if isinstance(item, str):
        return redact_url(item)
    return item


def list_assets(item, *, subtitles: bool, thumbnails: bool) -> None:
    """Print descriptive asset fields without exposing source credentials."""
    if subtitles:
        print("LANGUAGE\tTYPE\tFORMATS")
        for key, label in (("subtitles", "manual"), ("automatic_captions", "automatic")):
            groups = item.metadata.get(key) or {}
            if not isinstance(groups, dict):
                raise OmniError("Subtitle metadata must be a language dictionary")
            for language, tracks in groups.items():
                if not isinstance(tracks, list):
                    raise OmniError("Subtitle tracks must be a list")
                extensions = dict.fromkeys(str(track.get("ext") or "unknown")
                    for track in tracks if isinstance(track, dict))
                print("\t".join(map(display_text, [language, label, ", ".join(extensions)])))
    if thumbnails:
        print("THUMBNAIL\tRESOLUTION")
        images = item.metadata.get("thumbnails") or []
        if not isinstance(images, list):
            raise OmniError("Thumbnail metadata must be a list")
        for index, thumbnail in enumerate(images, 1):
            if isinstance(thumbnail, dict):
                print("\t".join(map(display_text, [thumbnail.get("id", index),
                    f"{thumbnail.get('width') or '?'}x{thumbnail.get('height') or '?'}"])))


def load_config(path: str, p: argparse.ArgumentParser) -> None:
    """Validate TOML defaults with the same value types as command-line options."""
    with open(path, "rb") as stream:
        document = tomllib.load(stream)
    config = document.get("omni")
    if not isinstance(config, dict):
        raise ValueError("configuration requires an [omni] table")
    allowed = {a.dest: a for a in p._actions if a.dest not in {"help", "version", "config", "urls"}}
    validated = {}
    for key, value in config.items():
        if key not in allowed:
            raise ValueError(f"unknown configuration key: {key}")
        action = allowed[key]
        if isinstance(action, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            if not isinstance(value, bool):
                raise ValueError(f"configuration {key} must be a boolean")
        elif isinstance(action, argparse._AppendAction):
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"configuration {key} must be an array of strings")
        elif action.type:
            if isinstance(value, bool) or not isinstance(value, (str, int, float)):
                raise ValueError(f"configuration {key} has the wrong value type")
            if action.type is int and isinstance(value, float):
                raise ValueError(f"configuration {key} must be an integer")
            value = action.type(str(value))
        elif not isinstance(value, str):
            raise ValueError(f"configuration {key} must be a string")
        if action.choices:
            choices = value if isinstance(value, list) else [value]
            if any(item not in action.choices for item in choices):
                raise ValueError(f"invalid configuration value for {key}")
        validated[key] = value
    p.set_defaults(**validated)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "compat":
        from .compat import run
        try:
            return run(args[1:])
        except (OmniError, OSError) as exc:
            message = str(exc) if isinstance(exc, OmniError) else "Could not start yt-dlp compatibility"
            print(f"ERROR: {message}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    inspecting = bool(args and args[0] == "inspect")
    if args and args[0] in {"inspect", "download"}:
        args.pop(0)
    p = parser()
    probe = argparse.ArgumentParser(add_help=False)
    probe.add_argument("--config")
    known, _ = probe.parse_known_args(args)
    if known.config:
        try:
            load_config(known.config, p)
        except (OSError, ValueError, TypeError, argparse.ArgumentTypeError) as exc:
            p.error(f"cannot load configuration ({type(exc).__name__})")
    opts = p.parse_args(args)
    if opts.list_extractors:
        from .extractors import BUILTINS
        for name, description in BUILTINS:
            print(f"{name}: {description}")
        print("generic: direct HTTP files, static HTML, RSS/Atom and JSON feeds")
        return 0
    if opts.retries < 0 or opts.timeout <= 0 or not math.isfinite(opts.timeout):
        p.error("retries must be nonnegative and timeout must be finite and positive")
    if opts.extract_audio and opts.remux_video:
        p.error("choose either --extract-audio or --remux-video")
    if opts.skip_download and (opts.embed_metadata or opts.embed_chapters or opts.embed_subs):
        p.error("embedding requires the main media; remove --skip-download")
    urls = opts.urls
    try:
        if opts.batch_file:
            if opts.batch_file == "-":
                lines = sys.stdin.read().splitlines()
            else:
                lines = Path(opts.batch_file).read_text(encoding="utf-8").splitlines()
            urls.extend(line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#"))
        if not urls:
            p.error("provide a URL or --batch-file")
        headers = {}
        for header in opts.add_header:
            name, sep, value = header.partition(":")
            if not sep or not name.strip() or "\n" in header or "\r" in header:
                p.error("headers must use NAME:VALUE without newlines")
            headers[name.strip()] = value.strip()
        if opts.user_agent:
            headers["User-Agent"] = opts.user_agent
        if opts.referer:
            headers["Referer"] = opts.referer
        from .engine import Downloader, Options
        from .assets import AssetOptions
        from .collection_selection import CollectionOptions
        from .transfer import OUTPUT_SIDECARS, TransferOptions
        collection = CollectionOptions(playlist_items=opts.playlist_items,
            playlist_start=opts.playlist_start, playlist_end=opts.playlist_end,
            playlist_reverse=opts.playlist_reverse, playlist_random=opts.playlist_random,
            random_seed=opts.playlist_random_seed, date=opts.date, datebefore=opts.datebefore,
            dateafter=opts.dateafter, match_filter=opts.match_filter)
        collection_filtering = any((opts.playlist_items is not None, opts.playlist_start != 1,
            opts.playlist_end is not None, opts.date, opts.datebefore, opts.dateafter, opts.match_filter))
        engine = Downloader(Options(
            transfer=TransferOptions(directory=opts.directory, output=opts.output, retries=opts.retries,
                timeout=opts.timeout, resume=opts.resume, overwrite=opts.force_overwrites,
                rate_limit=opts.limit_rate, max_filesize=opts.max_filesize, headers=headers, cookies=opts.cookies),
            media_types=set(opts.media_type) if opts.media_type else None, jobs=opts.jobs,
            max_items=opts.max_items, archive=opts.download_archive,
            audio_format=opts.audio_format if opts.extract_audio else None,
            remux_format=opts.remux_video, plugins=not opts.no_plugins, direct=opts.direct,
            format=opts.format, format_sort=opts.format_sort, merge_output_format=opts.merge_output_format,
            collection=collection if collection != CollectionOptions() else None, assets=AssetOptions(write_subs=opts.write_subs,
                write_auto_subs=opts.write_auto_subs, sub_langs=opts.sub_langs, sub_format=opts.sub_format,
                write_thumbnail=opts.write_thumbnail, write_all_thumbnails=opts.write_all_thumbnails,
                write_description=opts.write_description),
            embed_tags=opts.embed_metadata, embed_chapters=opts.embed_chapters,
            embed_subtitles=opts.embed_subs, reserved_paths=[opts.manifest] if opts.manifest else [],
            metadata_only=opts.skip_download or ((opts.list_subs or opts.list_thumbnails) and not opts.list_formats)))
        errors = 0
        def error(media, exc):
            nonlocal errors
            errors += 1
            # Native exceptions have actionable sanitized messages; arbitrary plugin exceptions do not.
            message = str(exc) if isinstance(exc, OmniError) else type(exc).__name__
            if opts.progress_json:
                print(json.dumps({"schema_version": 1, "event": "error", "id": media.id if media else "", "error": message}), file=sys.stderr, flush=True)
            else:
                print(f"ERROR: {message}", file=sys.stderr, flush=True)
        def complete(result):
            # An archive hit may refer to an older directory. Validate the
            # actual result, not only today's planned output paths.
            actual_paths = {str(Path(item.path + suffix).resolve()).casefold()
                for item in [result, *result.attachments] for suffix in OUTPUT_SIDECARS}
            if opts.manifest and str(Path(opts.manifest).resolve()).casefold() in actual_paths:
                raise OmniError("Manifest path overlaps the actual media output or sidecar")
            if opts.download_archive:
                archive = str(Path(opts.download_archive).expanduser().resolve()).casefold()
                if actual_paths.intersection({archive + suffix for suffix in ("", "-wal", "-shm", "-journal")}):
                    raise OmniError("Actual media output or sidecar overlaps the download archive")
            record = public_record(result.to_dict())
            if opts.write_info_json:
                from .infojson import write_info_json
                write_info_json(Path(result.path + ".info.json"), record, overwrite=opts.force_overwrites)
            if opts.manifest:
                with open(opts.manifest, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
            if opts.progress_json:
                print(json.dumps({"schema_version": 1, "event": "complete", **record}), file=sys.stderr, flush=True)
            elif not opts.quiet:
                print(f"{'Verified' if result.skipped else 'Downloaded'}: {result.path}", flush=True)
                for attachment in result.attachments:
                    print(f"{'Verified' if attachment.skipped else 'Downloaded'}: {attachment.path}", flush=True)
        media = []
        for url in dict.fromkeys(urls):
            try:
                found = engine.inspect(url)
                if not found:
                    if collection_filtering:
                        continue
                    raise OmniError("No matching media found. Site-specific extraction may require 'omni-dlp compat URL'.")
                media.extend(found)
            except (OmniError, OSError, ValueError) as exc:
                error(None, exc)
                if not opts.ignore_errors:
                    return 1
        if opts.list_formats or opts.list_subs or opts.list_thumbnails:
            for item in media:
                print(f"{display_text(item.id or 'media')} // {display_text(item.title)}")
                list_assets(item, subtitles=opts.list_subs, thumbnails=opts.list_thumbnails)
                if not opts.list_formats:
                    continue
                print("ID\tEXT\tRESOLUTION\tVCODEC\tACODEC\tKBIT/S\tPROTOCOL")
                from .selection import sort_formats
                for fmt in sort_formats(item.formats, opts.format_sort):
                    resolution = f"{fmt.width or '?'}x{fmt.height or '?'}" if fmt.height else "audio" if fmt.vcodec == "none" else "unknown"
                    print("\t".join(map(display_text, [fmt.format_id, fmt.ext, resolution, fmt.vcodec or "?", fmt.acodec or "?", fmt.tbr or fmt.abr or "?", fmt.protocol])))
                if not item.formats and item.metadata.get("formats_required"):
                    print("unavailable: no playable format candidates")
                elif not item.formats:
                    print(f"direct\t{display_text(item.ext)}\t{display_text(item.kind)}\t?\t?\t?\tHTTP")
        elif inspecting or opts.dump_json or opts.simulate:
            output = engine.prepare(media, on_error=error if opts.ignore_errors else None) if opts.format or opts.simulate else media
            for item in output:
                print(json.dumps(public_record(item.to_dict()), ensure_ascii=False), flush=True)
        else:
            if not opts.skip_download:
                media = engine.prepare(media, on_error=error if opts.ignore_errors else None)
            if opts.manifest:
                manifest = Path(opts.manifest).resolve()
                if opts.download_archive:
                    archive = str(Path(opts.download_archive).expanduser().resolve()).casefold()
                    if str(manifest).casefold() in {archive + suffix for suffix in ("", "-wal", "-shm", "-journal")}:
                        raise OmniError("Manifest path overlaps the download archive")
                for item in media:
                    for destination in engine.output_paths(item):
                        related = (Path(str(destination) + suffix) for suffix in OUTPUT_SIDECARS)
                        if any(str(manifest).casefold() == str(path).casefold() for path in related):
                            raise OmniError("Manifest path overlaps a media output or sidecar")
                manifest.parent.mkdir(parents=True, exist_ok=True)
            if opts.skip_download:
                engine.fetch_assets(media, on_result=complete, on_error=error if opts.ignore_errors else None)
                if opts.write_info_json:
                    from .infojson import write_info_json
                    for item in media:
                        base = engine._base_output_paths(item)[-1]
                        write_info_json(Path(str(base) + ".info.json"),
                            {"media": public_record(item.to_dict()), "downloaded": False},
                            overwrite=opts.force_overwrites)
            else:
                engine.fetch(media, on_result=complete, on_error=error if opts.ignore_errors else None)
        return 1 if errors else 0
    except KeyboardInterrupt:
        print("Interrupted; resumable partial downloads are retained.", file=sys.stderr)
        return 130
    except (OmniError, OSError, ValueError) as exc:
        message = str(exc) if isinstance(exc, OmniError) else type(exc).__name__
        if opts.progress_json:
            print(json.dumps({"schema_version": 1, "event": "error", "id": "", "error": message}), file=sys.stderr, flush=True)
        else:
            print(f"ERROR: {message}", file=sys.stderr)
        return 1
