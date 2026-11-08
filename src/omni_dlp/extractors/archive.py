"""Internet Archive items through the public metadata API, without upstream code."""
from __future__ import annotations

import hashlib
import math
import mimetypes
import re
from collections import OrderedDict
from urllib.parse import quote, unquote, urlsplit

from ..models import Format, Media, OmniError
from ..types import EXTENSION_KINDS, classify
from .common import ExtractorContext

_HOSTS = {"archive.org", "www.archive.org"}
_METADATA_FORMATS = {"metadata"}


def _location(url: str) -> tuple[str, str | None] | None:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in _HOSTS
                or parsed.username is not None or parsed.password is not None
                or parsed.port not in {None, 80, 443}):
            return None
        path = parsed.path.strip("/").split("/", 2)
        if len(path) < 2 or path[0] not in {"details", "embed", "download"}:
            return None
        identifier = unquote(path[1])
        if (not identifier or identifier in {".", ".."} or len(identifier) > 512
                or any(c in identifier for c in "/\\") or any(ord(c) < 32 for c in identifier)):
            return None
        filename = unquote(path[2]) if len(path) == 3 else None
        if filename is not None and not _safe_filename(filename):
            return None
        return identifier, filename
    except (TypeError, ValueError):
        return None


def supports(url: str) -> bool:
    return _location(url) is not None


def _safe_filename(value) -> bool:
    return (isinstance(value, str) and bool(value) and len(value) <= 4096
            and not value.startswith(("/", "\\")) and "\\" not in value
            and not any(part in {"", ".", ".."} for part in value.split("/"))
            and not any(ord(c) < 32 or ord(c) == 127 for c in value))


def _truth(value) -> bool:
    return value is True or (isinstance(value, (str, int)) and str(value).strip().lower() in {"true", "1", "yes"})


def _number(value, *, integer: bool = False):
    try:
        if isinstance(value, bool):
            return None
        result = float(value)
        if not math.isfinite(result) or result < 0:
            return None
        return int(result) if integer and result.is_integer() else (None if integer else result)
    except (TypeError, ValueError, OverflowError):
        return None


def _duration(value):
    if isinstance(value, str) and ":" in value:
        parts = value.split(":")
        if len(parts) > 3:
            return None
        numbers = [_number(p) for p in parts]
        if any(p is None for p in numbers):
            return None
        result = 0.0
        for part in numbers:
            result = result * 60 + part
        return result
    return _number(value)


def _text(value, fallback: str = "") -> str:
    if isinstance(value, list):
        return ", ".join(x for x in value if isinstance(x, str)) or fallback
    return value if isinstance(value, str) and value else fallback


def _kind(file: dict) -> tuple[str, str]:
    kind, extension = classify(file["name"])
    # Archive often labels Ogg movies by codec instead of using the .ogv suffix.
    label = _text(file.get("format")).lower()
    if extension in {"ogg", "oga"} and ("theora" in label or (_number(file.get("height")) or 0) > 0):
        kind = "video"
    return kind, extension


def _format_id(name: str, extension: str) -> str:
    reserved = {"all", "mergeall", "best", "worst", "bestvideo", "bestaudio", "worstvideo", "worstaudio",
                "b", "w", "bv", "ba", "wv", "wa", *EXTENSION_KINDS}
    if re.fullmatch(r"[A-Za-z0-9_.-]+", name) and name.lower() not in reserved:
        return name
    return "file-" + (extension or "bin") + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]


def _format(file: dict, identifier: str, context: ExtractorContext, kind: str) -> Format:
    name = file["name"]
    url = f"https://archive.org/download/{quote(identifier, safe='')}/{quote(name, safe='/')}"
    _, extension = _kind(file)
    duration = _duration(file.get("length"))
    size = _number(file.get("size"), integer=True)
    bitrate = _number(file.get("bitrate"))
    if bitrate is None and size is not None and duration:
        bitrate = size * 8 / duration / 1000
    label = _text(file.get("format"))
    if bitrate is None:
        match = re.search(r"(\d+)\s*Kbps", label, re.I)
        bitrate = float(match[1]) if match else None
    codecs = {
        "mp3": "mp3", "flac": "flac", "wav": "pcm", "ogg": "vorbis",
        "oga": "vorbis", "opus": "opus", "m4a": "aac", "aac": "aac",
    }
    metadata = {"filename": name, "source": _text(file.get("source")), "format_label": label}
    if duration is not None:
        metadata["duration"] = duration
    for checksum in ("md5", "sha1", "crc32"):
        value = file.get(checksum)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8,64}", value):
            metadata[checksum] = value.lower()
    if kind == "audio":
        metadata["audio_channels"] = _number(file.get("channels"), integer=True)
    return Format(url=url, format_id=_format_id(name, extension), ext=extension,
                  vcodec="none" if kind != "video" else "",
                  acodec=codecs.get(extension, "") if kind == "audio" else ("" if kind == "video" else "none"),
                  width=_number(file.get("width"), integer=True), height=_number(file.get("height"), integer=True),
                  fps=_number(file.get("fps")), tbr=bitrate, abr=bitrate if kind == "audio" else None,
                  filesize=size, headers=context.scoped_headers(url), metadata=metadata)


def extract(url: str, context: ExtractorContext) -> list[Media]:
    """Discover one item's public files, grouping same-kind AV derivatives.

    Metadata records are omitted from item discovery; an exact file URL may
    select one explicitly. Restriction markers are reported, never bypassed.
    """
    location = _location(url)
    if location is None:
        raise OmniError("Unsupported Internet Archive URL")
    if type(context.max_items) is not int or context.max_items < 1:
        raise OmniError("max_items must be positive")
    identifier, selected = location
    data = context.get_json(f"https://archive.org/metadata/{quote(identifier, safe='')}?extended_err=1")
    if not data:
        raise OmniError("Internet Archive item was not found")
    if not isinstance(data, dict):
        raise OmniError("Internet Archive returned an invalid item record")
    if data.get("error"):
        raise OmniError("Internet Archive reports that this item is unavailable")
    metadata = data.get("metadata", {})
    if not isinstance(metadata, dict):
        raise OmniError("Internet Archive returned invalid item metadata")
    if _truth(data.get("is_dark")) or _truth(metadata.get("is_dark")):
        raise OmniError("Internet Archive item is private or unavailable")
    if _truth(metadata.get("access-restricted-item")):
        raise OmniError("Internet Archive item is access-restricted; native entitlement handling is not implemented")
    if metadata.get("mediatype") == "collection":
        raise OmniError("Internet Archive collection traversal is not implemented")
    records = data.get("files")
    if not isinstance(records, list):
        raise OmniError("Internet Archive item has no valid file list")
    files: dict[str, dict] = {}
    omitted = {"private": 0, "metadata": 0, "invalid": 0}
    for file in records:
        if not isinstance(file, dict) or not _safe_filename(file.get("name")):
            omitted["invalid"] += 1
            continue
        name = file["name"]
        if selected is not None and name != selected:
            continue
        if _truth(file.get("private")):
            omitted["private"] += 1
            continue
        label = _text(file.get("format")).lower()
        if selected is None and (file.get("source") == "metadata" or label in _METADATA_FORMATS):
            omitted["metadata"] += 1
            continue
        if name in files:
            if files[name] != file:
                raise OmniError("Internet Archive returned conflicting records for the same filename")
            continue
        files[name] = file
    if selected is not None and not files:
        if omitted["private"]:
            raise OmniError("Requested Internet Archive file is private")
        raise OmniError("Requested Internet Archive file was not found")

    def original(name: str, kind: str) -> str:
        if kind not in {"audio", "video"}:
            return name
        seen: set[str] = set()
        cursor = name
        while cursor not in seen:
            seen.add(cursor)
            parent = files[cursor].get("original")
            if not isinstance(parent, str) or parent not in files or _kind(files[parent])[0] != kind:
                return cursor
            cursor = parent
        # A malformed derivative cycle must not lose files or hang traversal.
        return name

    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for name, file in files.items():
        kind, _ = _kind(file)
        if context.media_types and kind not in context.media_types:
            continue
        key = original(name, kind)
        groups.setdefault(key, []).append(file)
    total = len(groups)
    results = []
    for index, (name, group) in enumerate(groups.items(), start=1):
        if len(results) >= context.max_items:
            break
        source = files.get(name, group[0])
        kind, extension = _kind(source)
        # Stable metadata ordering makes repeated extraction reproducible.
        group.sort(key=lambda file: (file["name"] != name, file["name"]))
        formats = [_format(file, identifier, context, kind) for file in group]
        primary = formats[0]
        unique_id = identifier + "-" + hashlib.sha256(name.encode()).hexdigest()[:12]
        item_metadata = {
            "extractor": "internetarchive", "archive_id": f"internetarchive:{identifier}/{name}",
            "item_identifier": identifier, "filename": name,
            "item_title": _text(metadata.get("title"), identifier),
            "description": _text(metadata.get("description")), "creator": _text(metadata.get("creator")),
            "license_url": _text(metadata.get("licenseurl")),
            "playlist_id": identifier, "playlist_index": index, "playlist_count": total,
            "discovery_limited": total > context.max_items,
            "omitted_files": dict(omitted),
        }
        duration = _duration(source.get("length"))
        if duration is not None:
            item_metadata["duration"] = duration
        for checksum in ("md5", "sha1", "crc32"):
            value = source.get(checksum)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{8,64}", value):
                item_metadata[checksum] = value.lower()
        results.append(Media(
            url=primary.url, title=_text(source.get("title"), name.rsplit("/", 1)[-1].rsplit(".", 1)[0]),
            id=unique_id, kind=kind, ext=extension,
            mime_type="video/ogg" if kind == "video" and extension == "ogg" else (mimetypes.guess_type(name)[0] or ""),
            source_url=context.source_url,
            headers=primary.headers, metadata=item_metadata,
            formats=formats if kind in {"audio", "video"} else [],
        ))
    if not results and omitted["private"]:
        raise OmniError("Internet Archive item has no matching public files; private files were omitted")
    return results
