"""Clear adaptive streams and local media conversion using an optional FFmpeg.

The native adaptive path deliberately has fewer options than HTTP transfers.
It never forwards credentials to arbitrary hosts referenced by a manifest.
"""
from __future__ import annotations

import os
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
        errors.seek(0, os.SEEK_END)
        if code or errors.tell():
            # FFmpeg can skip unavailable segments and still return zero. Treat
            # warnings as incomplete media rather than silently accepting gaps.
            # FFmpeg errors contain request URLs, cookies, and local filenames.
            # Do not expose its raw stderr in API errors or application logs.
            raise OmniError(
                f"FFmpeg reported an error (exit {code}); verify the stream, available codecs, and access. "
                "Native adaptive downloading supports clear HTTP(S) streams only"
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


def download_stream(media: Media, options: TransferOptions | None = None) -> DownloadResult:
    """Remux clear HLS/DASH to MKV, restarting interrupted adaptive transfers.

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
