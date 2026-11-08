"""Download selected representations and merge their local streams atomically.

The transfer layer fetches each representation independently. FFmpeg sees only
verified local files and cannot request URLs or read playlist references while
merging them. Source components remain available for retry and inspection.
"""
from __future__ import annotations

from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import shutil

from .models import DownloadResult, Format, Media, OmniError
from .streams import (
    _CONTAINERS, _LOCAL_FORMATS, _check_cancelled, _execute, _ffmpeg, _publish,
    _temporary, download_stream,
)
from .transfer import (
    TransferOptions, _destination_lock, _origin, _safe_component,
    check_existing, download, file_digest, output_path, record_complete, validate_url,
)
from .types import classify

_SAFE_HEADERS = {"user-agent", "accept", "accept-language", "accept-encoding"}
_LOCAL_INPUT_FORMATS = _LOCAL_FORMATS + ",srt,webvtt,ass"
_RECEIPT_SUFFIXES = ("", ".part", ".part.json", ".omni.json", ".omni.lock", ".info.json")
_ADAPTIVE_PROTOCOLS = {"hls", "dash", "m3u8", "m3u8_native"}


def _adaptive(component: Media) -> bool:
    return component.metadata.get("protocol") in _ADAPTIVE_PROTOCOLS or component.ext in {"m3u8", "mpd"}


def _formats(media: Media) -> list[Format]:
    formats = media.requested_formats
    if len(formats) < 2:
        raise OmniError("Merging requires at least two selected formats")
    if not all(isinstance(item, Format) for item in formats):
        raise OmniError("Selected formats must be Format records")
    for item in formats:
        validate_url(item.url)
        if item.protocol not in {"", "http", "https", *_ADAPTIVE_PROTOCOLS}:
            raise OmniError("Selected format uses an unsupported native transfer protocol")
        if item.metadata.get("drm") or item.metadata.get("has_drm"):
            raise OmniError("DRM-protected selected formats are unsupported")
    return formats


def _kind(fmt: Format) -> str:
    if fmt.metadata.get("kind") == "subtitle":
        return "subtitle"
    if fmt.vcodec not in {"", "none"}:
        return "video"
    if fmt.acodec not in {"", "none"} and fmt.vcodec == "none":
        return "audio"
    kind, _ = classify(fmt.url, fmt.metadata.get("mime_type", ""), "file." + fmt.ext if fmt.ext else "")
    return kind if kind in {"video", "audio", "subtitle"} else "video"


def component_media(media: Media) -> list[Media]:
    """Expand selected formats without forwarding source-origin credentials."""
    components = []
    for fmt in _formats(media):
        origin_url = media.source_url or media.url
        inherited = media.headers
        if _origin(origin_url) != _origin(fmt.url):
            inherited = {key: value for key, value in inherited.items() if key.lower() in _SAFE_HEADERS}
        _, guessed = classify(fmt.url)
        extension = fmt.ext or guessed or "bin"
        if not re.fullmatch(r"[A-Za-z0-9]{1,12}", extension):
            raise OmniError("Selected format has an invalid file extension")
        metadata = {**media.metadata, **fmt.metadata, "format_id": fmt.format_id,
                    "protocol": fmt.protocol, "vcodec": fmt.vcodec, "acodec": fmt.acodec}
        archive_id = media.metadata.get("archive_id")
        if archive_id and fmt.format_id:
            metadata["resource_id"] = f"{archive_id}|format:{fmt.format_id}"
        else:
            metadata.pop("resource_id", None)
        components.append(replace(
            media, url=fmt.url, kind=_kind(fmt), ext=extension.lower(),
            source_url=origin_url, mime_type=fmt.metadata.get("mime_type", ""),
            headers={**inherited, **fmt.headers}, formats=[], requested_formats=[],
            metadata=metadata,
        ))
    return components


def _literal_options(options: TransferOptions, path: Path, component: Media) -> TransferOptions:
    root = Path(options.directory).expanduser().resolve()
    relative = path.relative_to(root).as_posix()
    headers = options.headers
    if component.source_url and _origin(component.source_url) != _origin(component.url):
        headers = {key: value for key, value in headers.items() if key.lower() in _SAFE_HEADERS}
    return replace(options, output=relative.replace("%", "%%"), headers=headers)


def _layout(media: Media, options: TransferOptions, container: str):
    if not isinstance(container, str) or container.lower() not in _CONTAINERS:
        raise OmniError("Unsupported merge container; choose " + ", ".join(sorted(_CONTAINERS)))
    container = container.lower()
    parts = component_media(media)
    merged = replace(media, ext=container, mime_type="", metadata={**media.metadata, "merged": True})
    destination = output_path(merged, options)
    actual_container = destination.suffix.lstrip(".").lower() or container
    if actual_container not in _CONTAINERS:
        raise OmniError("Merge output must have a supported container extension")
    merged = replace(merged, ext=actual_container,
                     kind="video" if any(part.kind == "video" for part in parts) else "audio")
    tokens: set[str] = set()
    paths: list[Path] = []
    for index, part in enumerate(parts, 1):
        value = _safe_component(part.metadata.get("format_id") or str(index))
        token = "f" + value.encode("utf-8")[:48].decode("utf-8", errors="ignore").rstrip(" .")
        original = token
        suffix = 1
        while token.casefold() in tokens:
            suffix += 1
            token = original + "." + str(suffix)
        tokens.add(token.casefold())
        extension = "mkv" if _adaptive(part) else part.ext
        tail = "." + token + "." + extension
        stem = destination.stem.encode("utf-8")[:240 - len(tail.encode("utf-8"))].decode("utf-8", errors="ignore")
        candidate = destination.with_name(stem.rstrip(" .") + tail)
        # Revalidate the derived name and all its journals through the same
        # confined-path implementation used by standalone HTTP downloads.
        paths.append(output_path(part, _literal_options(options, candidate, part)))
    paths.append(destination)
    occupied: set[str] = set()
    for path in paths:
        related = {str(path).casefold() + suffix for suffix in _RECEIPT_SUFFIXES}
        if occupied.intersection(related):
            raise OmniError("Selected format outputs overlap another output or sidecar")
        occupied.update(related)
    return merged, parts, paths, actual_container


def planned_paths(media: Media, options: TransferOptions, container: str = "mkv") -> list[Path]:
    """Return retained component paths followed by the merged output path."""
    return _layout(media, options, container)[2]


def _maps(formats: list[Format], components: list[Media]) -> list[str]:
    arguments: list[str] = []
    for index, (fmt, part) in enumerate(zip(formats, components)):
        if part.kind == "subtitle":
            arguments += ["-map", f"{index}:s:0"]
            continue
        if fmt.vcodec == "none" and fmt.acodec == "none":
            raise OmniError("Selected format declares no audio or video streams")
        if fmt.vcodec != "none":
            # Known codec means a missing stream is an error. Unknown streams
            # are optional, but a wholly empty merge still fails in FFmpeg.
            arguments += ["-map", f"{index}:v:0" + ("?" if not fmt.vcodec else "")]
        if fmt.acodec != "none":
            arguments += ["-map", f"{index}:a:0" + ("?" if not fmt.acodec else "")]
    return arguments


def download_merged(media: Media, options: TransferOptions, *, container: str = "mkv") -> DownloadResult:
    """Fetch every selected component, then copy its streams into one container.

    Each component and the final output honor max_filesize independently. The
    source files are retained. Every explicitly selected track is preserved;
    callers enforce any desired policy for duplicate audio/video stream types.
    """
    _check_cancelled(options.cancel_event)
    merged, parts, paths, container = _layout(media, options, container)
    maps = _maps(media.requested_formats, parts)
    _ffmpeg()  # Missing FFmpeg must fail before downloading source components.
    results: list[DownloadResult] = []
    for part, path in zip(parts, paths[:-1]):
        _check_cancelled(options.cancel_event)
        settings = _literal_options(options, path, part)
        result = download_stream(part, settings) if _adaptive(part) else download(part, settings)
        if Path(result.path) != path:
            raise OmniError("Component transfer returned an unexpected destination")
        results.append(result)
    destination = paths[-1]
    with ExitStack() as stack:
        # Deterministic order avoids lock-order inversion across processes.
        for path in sorted(paths, key=lambda p: str(p).casefold()):
            stack.enter_context(_destination_lock(path))
        profile = []
        for fmt, result in zip(media.requested_formats, results):
            _check_cancelled(options.cancel_event)
            path = Path(result.path)
            if not path.is_file() or path.is_symlink():
                raise OmniError("Merge input must be a regular downloaded file")
            digest = file_digest(path)
            if digest != result.sha256:
                raise OmniError("Merge input changed after download verification")
            profile.append({"format_id": fmt.format_id, "sha256": digest,
                            "vcodec": fmt.vcodec, "acodec": fmt.acodec})
        signature = json.dumps({"container": container, "parts": profile}, sort_keys=True, separators=(",", ":"))
        variant = "merge-v1:" + hashlib.sha256(signature.encode()).hexdigest()
        existing = check_existing(merged, destination, options.overwrite, variant=variant)
        if existing is not None:
            return existing
        total = sum(result.size for result in results)
        if shutil.disk_usage(destination.parent).free < total:
            raise OmniError("Insufficient disk space for the merged output")
        arguments = []
        for path in paths[:-1]:
            arguments += ["-protocol_whitelist", "file", "-format_whitelist", _LOCAL_INPUT_FORMATS,
                          "-i", str(path.resolve())]
        arguments += [*maps, "-c", "copy", "-f", _CONTAINERS[container]]
        temporary = _temporary(destination)
        try:
            _execute(arguments, temporary, options.max_filesize, cancel_event=options.cancel_event)
            _check_cancelled(options.cancel_event)
            _publish(temporary, destination, options.overwrite)
            return record_complete(merged, destination, variant=variant)
        finally:
            temporary.unlink(missing_ok=True)
