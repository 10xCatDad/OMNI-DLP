"""Adaptive streams and local media conversion using an optional FFmpeg.

The native adaptive path deliberately has fewer options than HTTP transfers.
It never forwards credentials to arbitrary hosts referenced by a manifest.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from .models import DownloadResult, Media, OmniError

if TYPE_CHECKING:
    from threading import Event

    from .transfer import TransferOptions

_AUDIO = {
    "mp3": "libmp3lame", "m4a": "aac", "m4b": "aac", "aac": "aac", "flac": "flac",
    "opus": "libopus", "ogg": "libvorbis", "wav": "pcm_s16le", "aiff": "pcm_s16be",
}
_REMUX = {"mkv", "mp4", "mov", "webm", "ogg", "flv"}
_CONTAINERS = {"mkv": "matroska", "mp4": "mp4", "mov": "mov", "webm": "webm", "ogg": "ogg", "flv": "flv"}
_SAFE_HEADERS = {"user-agent", "accept", "accept-language", "accept-encoding"}
_LOCAL_FORMATS = "mov,mp3,flac,matroska,webm,ogg,wav,aiff,aac,mpeg,mpegts,avi,flv,asf"
# Some origins serve valid HLS using application/octet-stream. This exact
# FFmpeg diagnostic says nothing about missing/corrupt media. Never allow a
# substring match that could swallow a separate error on the same line.
_BENIGN_HLS_MIME = re.compile(
    rb"(?:\[hls @ (?:0x)?[0-9a-fA-F]+\] )?mime type is not rfc8216 compliant"
)


def _ffmpeg() -> str:
    executable = shutil.which("ffmpeg")
    if not executable:
        raise OmniError("FFmpeg is required for adaptive streams and conversion; install ffmpeg and retry")
    return executable


def _temporary(destination: Path) -> Path:
    fd, name = tempfile.mkstemp(prefix=".omni-", suffix="." + destination.suffix.lstrip("."), dir=destination.parent)
    os.close(fd)
    return Path(name)


def _check_cancelled(cancel_event: Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise OmniError("Download cancelled")


def _execute(
    arguments: list[str], temporary: Path, max_filesize: int | None = None, *,
    strict: bool = False, cancel_event: Event | None = None,
) -> None:
    """Bound memory/log output and stop oversized transfers without committing them."""
    _check_cancelled(cancel_event)
    with tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(
                [_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "warning" if strict else "error", "-y", *arguments, str(temporary)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
            )
        except OSError as exc:
            raise OmniError("Could not start FFmpeg") from exc
        try:
            while True:
                _check_cancelled(cancel_event)
                if max_filesize is not None and temporary.stat().st_size > max_filesize:
                    raise OmniError("Adaptive media exceeds the configured maximum file size")
                if errors.tell() > 65536:
                    raise OmniError("FFmpeg reported excessive errors; output was not committed")
                try:
                    code = process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1 if cancel_event is not None and cancel_event.is_set() else 5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        _check_cancelled(cancel_event)
        errors.seek(0)
        diagnostics = errors.read(65537)
        unexpected = bool(diagnostics) and (not strict or any(
            _BENIGN_HLS_MIME.fullmatch(line) is None for line in diagnostics.splitlines()
        ))
        if code or len(diagnostics) > 65536 or unexpected:
            # FFmpeg can skip unavailable segments and still return zero. Reject
            # every diagnostic except the exact benign HLS MIME warning above.
            # FFmpeg errors contain request URLs, cookies, and local filenames.
            # Do not expose its raw stderr in API errors or application logs.
            raise OmniError(
                f"FFmpeg reported an error (exit {code}); verify the stream, available codecs, and access. "
                "Verify segment and key availability; native HLS supports clear media and ordinary AES-128 only"
            )
        size = temporary.stat().st_size
        if not size:
            raise OmniError("FFmpeg produced no media")
        if max_filesize is not None and size > max_filesize:
            raise OmniError("Adaptive media exceeds the configured maximum file size")


def _publish(temporary: Path, destination: Path, overwrite: bool) -> None:
    if overwrite:
        if destination.is_symlink():
            raise OmniError("Refusing to replace a symbolic-link destination")
        os.replace(temporary, destination)
    else:
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise OmniError("Output appeared during conversion; refusing to overwrite it") from exc
        except OSError as exc:
            raise OmniError("Could not atomically publish the converted media") from exc
        temporary.unlink()



def _preflight_hls(media: Media, options: TransferOptions, headers: dict[str, str]) -> bool:
    """Validate bounded initial manifests and determine whether crypto is needed.

    This is an initial-document check, not an immutable manifest proxy. FFmpeg
    fetches manifests again and may reload live playlists. Its protocol whitelist
    continues to prohibit file/data resources regardless of subsequent changes.
    """
    from .extractors.common import ExtractorContext
    from .manifests import _attributes, _uri, parse_hls

    context = ExtractorContext(media.source_url or media.url, headers=headers, timeout=options.timeout)
    pending = [(media.url, frozenset())]
    visited = set()
    keys = set()
    total_bytes = 0
    encrypted = False
    while pending:
        _check_cancelled(options.cancel_event)
        target, ancestry = pending.pop()
        if target in ancestry:
            raise OmniError("HLS master manifests contain a reference cycle")
        if target in visited:
            continue
        if len(visited) >= 64:
            raise OmniError("HLS preflight exceeds 64 manifest documents; select a specific variant")
        visited.add(target)
        data, final_url, outgoing = context.get_document(target)
        if final_url in ancestry:
            raise OmniError("HLS master manifests contain a reference cycle")
        total_bytes += len(data)
        if total_bytes > 32 * 1024 * 1024:
            raise OmniError("HLS preflight exceeds the cumulative document limit")
        try:
            text = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise OmniError("HLS manifest is not valid UTF-8") from exc
        formats = parse_hls(text, final_url, headers=outgoing)
        encrypted = encrypted or any(item.metadata.get("encryption") == "AES-128" for item in formats)
        # Ordinary AES-128 keys are exactly 16 bytes. Verify availability and
        # length without writing key bytes, URLs, or response data to receipts.
        for line in text.splitlines():
            line = line.strip()
            if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
                attributes = _attributes(line.partition(':')[2])
                if attributes.get("METHOD") != "AES-128":
                    continue
                key_url = _uri(attributes["URI"], final_url)
                if key_url in keys:
                    continue
                if len(keys) >= 256:
                    raise OmniError("HLS preflight exceeds 256 distinct AES-128 keys")
                _check_cancelled(options.cancel_event)
                key_context = replace(context, max_document=16)
                try:
                    key = key_context.get_bytes(key_url)
                except OmniError as exc:
                    raise OmniError("HLS AES-128 key is unavailable or invalid; check key access and the required 16-byte length") from exc
                if len(key) != 16:
                    raise OmniError("HLS AES-128 key must contain exactly 16 bytes")
                keys.add(key_url)
        for item in formats:
            if "is_live" not in item.metadata:
                pending.append((item.url, ancestry | {target, final_url}))
    return encrypted


def download_stream(media: Media, options: TransferOptions | None = None) -> DownloadResult:
    """Remux HTTP(S) HLS/DASH to MKV, restarting interrupted adaptive transfers.

    HTTP resume and rate limits are not implemented by this backend. Its file
    size bound protects the final output, with small transient muxer overshoot.
    Cookie files and arbitrary headers require the explicit compatibility path.
    """
    from .transfer import TransferOptions, _destination_lock, check_existing, output_path, record_complete, validate_url

    options = options or TransferOptions()
    _check_cancelled(options.cancel_event)
    validate_url(media.url)
    if options.rate_limit is not None:
        raise OmniError("Native adaptive streams do not support rate limits; use compatibility mode")
    if options.cookies:
        raise OmniError("Native adaptive streams do not support cookie files; use compatibility mode")
    if options.timeout <= 0:
        raise OmniError("Timeout must be positive")
    if options.max_filesize is not None and options.max_filesize < 1:
        raise OmniError("Maximum file size must be positive")
    headers = {**media.headers, **options.headers}
    if any(name.lower() not in _SAFE_HEADERS for name in headers):
        raise OmniError("Native adaptive streams only accept User-Agent, Accept, Accept-Language, and Accept-Encoding headers; use compatibility mode for authentication")
    if any(any(ord(char) < 32 and char != "\t" or ord(char) == 127 for char in str(value)) for value in headers.values()):
        raise OmniError("Header values must not contain control characters")
    if media.metadata.get("drm") or media.metadata.get("has_drm"):
        raise OmniError("DRM-protected adaptive streams are unsupported")
    converted = replace(media, ext="mkv", mime_type="video/x-matroska")
    destination = output_path(converted, options)
    extension = destination.suffix.lstrip(".").lower() or "mkv"
    if extension not in _CONTAINERS:
        raise OmniError("Adaptive output must use a supported container extension: " + ", ".join(sorted(_CONTAINERS)))
    if extension != "mkv":
        converted = replace(converted, ext=extension, mime_type="")
    variant = f"adaptive:{_CONTAINERS[extension]}"
    arguments = [
        "-protocol_whitelist", "http,https,tcp,tls",
        "-format_whitelist", "hls,dash," + _LOCAL_FORMATS,
        "-rw_timeout", str(int(options.timeout * 1_000_000)),
    ]
    if headers:
        arguments += ["-headers", "".join(f"{name}: {value}\r\n" for name, value in headers.items())]
    arguments += ["-i", media.url, "-map", "0:v?", "-map", "0:a?", "-map", "0:s?", "-c", "copy", "-f", _CONTAINERS[extension]]
    with _destination_lock(destination):
        existing = check_existing(converted, destination, options.overwrite, variant=variant)
        if existing is not None:
            return existing
        _ffmpeg()
        from urllib.parse import urlsplit
        is_hls = media.metadata.get("protocol") == "hls" or media.ext.lower() == "m3u8" or urlsplit(media.url).path.lower().endswith(".m3u8")
        if is_hls:
            encrypted = _preflight_hls(media, options, headers)
            if encrypted:
                arguments[1] += ",crypto"
                converted = replace(converted, metadata={**converted.metadata, "encryption": "AES-128"})
        temporary = _temporary(destination)
        try:
            _execute(arguments, temporary, options.max_filesize, strict=True, cancel_event=options.cancel_event)
            _check_cancelled(options.cancel_event)
            _publish(temporary, destination, options.overwrite)
            return record_complete(converted, destination, variant=variant)
        finally:
            temporary.unlink(missing_ok=True)


def transcode(
    result: DownloadResult, *, audio_format: str | None = None, remux_format: str | None = None,
    overwrite: bool = False, cancel_event: Event | None = None,
) -> DownloadResult:
    """Extract audio or remux a local download, retaining the original file.

    Existing conversion outputs are replaced only with overwrite=True. Remote protocols and
    playlist demuxers are disabled when processing downloaded local media.
    """
    from .transfer import _destination_lock, check_existing, file_digest, record_complete

    _check_cancelled(cancel_event)
    if bool(audio_format) == bool(remux_format):
        raise OmniError("Choose exactly one audio format or remux format")
    audio_format = audio_format.lower() if audio_format else None
    remux_format = remux_format.lower() if remux_format else None
    if audio_format and audio_format not in _AUDIO:
        raise OmniError("Unsupported audio format; choose " + ", ".join(sorted(_AUDIO)))
    if remux_format and remux_format not in _REMUX:
        raise OmniError("Unsupported remux format; choose " + ", ".join(sorted(_REMUX)))
    source = Path(result.path)
    if not source.is_file() or source.is_symlink():
        raise OmniError("Conversion input must be a regular downloaded file")
    extension = audio_format or remux_format
    destination = source.with_suffix("." + str(extension))
    if destination == source:
        destination = source.with_name(source.stem + ".converted." + str(extension))
    converted = replace(
        result.media, ext=str(extension), kind="audio" if audio_format else result.media.kind,
        mime_type="", metadata={**result.media.metadata, "conversion": audio_format or remux_format},
    )
    arguments = ["-protocol_whitelist", "file", "-format_whitelist", _LOCAL_FORMATS, "-i", str(source.resolve())]
    if audio_format:
        arguments += ["-map", "0:a:0", "-vn", "-c:a", _AUDIO[audio_format]]
    else:
        arguments += ["-map", "0", "-c", "copy"]
    with _destination_lock(source), _destination_lock(destination):
        variant = f"audio={audio_format};remux={remux_format};source_sha256={file_digest(source)}"
        existing = check_existing(converted, destination, overwrite, variant=variant)
        if existing is not None:
            return existing
        temporary = _temporary(destination)
        try:
            _execute(arguments, temporary, cancel_event=cancel_event)
            _check_cancelled(cancel_event)
            _publish(temporary, destination, overwrite)
            return record_complete(converted, destination, variant=variant)
        finally:
            temporary.unlink(missing_ok=True)
