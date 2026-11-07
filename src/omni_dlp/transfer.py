"""Resumable, byte-preserving HTTP transfers with verifiable completion records."""
from __future__ import annotations

import contextlib
import hashlib
import http.client
import http.cookiejar
import json
import math
import os
import re
import shutil
import socket
import stat
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request
from urllib.request import build_opener as _build_opener

from .models import DownloadResult, Media, OmniError

_CHUNK = 64 * 1024
_RESERVED = re.compile(r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", re.I)
_TEMPLATE = re.compile(r"%\((\w+)\)(?:0?(\d+))?([sd])")


@dataclass
class TransferOptions:
    directory: str = "."
    output: str = "%(title)s [%(id)s].%(ext)s"
    retries: int = 3
    timeout: float = 30
    resume: bool = True
    overwrite: bool = False
    rate_limit: int | None = None
    max_filesize: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    cookies: str | None = None


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise OmniError("Invalid HTTP URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise OmniError("Native transfer requires an HTTP or HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise OmniError("URL credentials are unsupported; use an Authorization header or cookie file")
    if any(ord(char) < 32 or ord(char) == 127 for char in url):
        raise OmniError("Control characters are not allowed in URLs")
    if port is not None and port <= 0:
        raise OmniError("Invalid HTTP port")


def _origin(url: str) -> tuple[str, str | None, int | None]:
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def request_url(url: str) -> str:
    """Encode Unicode hosts and paths without changing stored source identity."""
    validate_url(url)
    parsed = urlsplit(url)
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OmniError("Invalid internationalized hostname") from exc
    if ":" in hostname:
        hostname = f"[{hostname}]"
    if parsed.port is not None:
        hostname += f":{parsed.port}"
    safe = "/:@!$&'()*+,;=-._~%"
    return urlunsplit((parsed.scheme, hostname, quote(parsed.path, safe=safe),
                       quote(parsed.query, safe=safe + "?"), ""))


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and _origin(req.full_url) != _origin(newurl):
            # Unknown custom headers may contain secrets too. Cookies for the new
            # origin are added separately by HTTPCookieProcessor.
            redirected.headers = {
                key: value for key, value in redirected.headers.items()
                if key.lower() in {"accept", "accept-encoding", "user-agent"}
            }
            redirected.unredirected_hdrs = {}
        return redirected


def build_opener(cookies: str | None = None):
    handlers = [_SafeRedirect()]
    if cookies:
        jar = http.cookiejar.MozillaCookieJar(cookies)
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise OmniError("Cannot load Netscape cookie file") from exc
        handlers.append(HTTPCookieProcessor(jar))
    return _build_opener(*handlers)


def request_headers(headers: dict[str, str] | None = None) -> dict[str, str]:
    result = {"User-Agent": "omni-dlp/0.1", "Accept-Encoding": "identity"}
    for name, value in (headers or {}).items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise OmniError("Invalid HTTP header name")
        if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for c in str(value)):
            raise OmniError("Invalid HTTP header value")
        try:
            str(value).encode("latin-1")
        except UnicodeEncodeError as exc:
            raise OmniError("HTTP header values must be Latin-1 encodable") from exc
        if name.lower() in {"range", "if-range", "content-length", "transfer-encoding", "host"}:
            raise OmniError(f"The transfer engine manages the {name} header")
        result = {key: val for key, val in result.items() if key.lower() != name.lower()}
        result[name] = str(value)
    # Transparent decompression would invalidate offsets and byte checksums.
    result = {key: val for key, val in result.items() if key.lower() != "accept-encoding"}
    result["Accept-Encoding"] = "identity"
    return result


def _source_hash(media: Media) -> str:
    return hashlib.sha256(media.url.encode()).hexdigest()


def _safe_component(value: object) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", str(value)).strip(" .")
    if value in {"", ".", ".."}:
        return "untitled"
    if _RESERVED.match(value):
        value = "_" + value
    # Keep each metadata expansion bounded even on byte-limited filesystems.
    raw = value.encode("utf-8")[:160]
    return raw.decode("utf-8", errors="ignore").rstrip(" .") or "untitled"


def _check_regular(path: Path) -> None:
    if path.is_symlink():
        raise OmniError("Refusing a symbolic-link output or sidecar")
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise OmniError("Output or sidecar is not a regular file")


def output_path(media: Media, options: TransferOptions) -> Path:
    """Render a confined output path; create safe parent directories."""
    root = Path(options.directory).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise OmniError("Cannot create download directory") from exc
    basename = unquote(Path(urlsplit(media.url).path).name)
    values = {
        "title": media.title or Path(basename).stem or "media",
        "id": media.id or _source_hash(media)[:12],
        "ext": media.ext.lstrip(".") or Path(basename).suffix.lstrip(".") or "bin",
        "kind": media.kind,
        "playlist_index": media.metadata.get("playlist_index", 1),
    }

    def substitute(match):
        name, width, conversion = match.groups()
        if name not in values:
            raise OmniError(f"Unsupported output template field: {name}")
        value = values[name]
        if width and int(width) > 20:
            raise OmniError("Output template field width is too large")
        if conversion == "d":
            try:
                value = f"{int(value):0{int(width or 0)}d}"
            except (ValueError, TypeError) as exc:
                raise OmniError(f"Output field {name} is not an integer") from exc
        return _safe_component(value)

    # Preserve literal %% without treating it as a format directive.
    if "\x00" in options.output:
        raise OmniError("Invalid output template")
    template = options.output.replace("%%", "\x00")
    if "%" in _TEMPLATE.sub("", template):
        raise OmniError("Invalid or unsupported output template syntax")
    rendered = _TEMPLATE.sub(substitute, template)
    rendered = rendered.replace("\x00", "%")
    if re.search(r'[<>:"\\|?*\x00-\x1f\x7f]', rendered):
        raise OmniError("Output templates must use relative portable paths")
    relative = Path(rendered)
    if relative.is_absolute() or not relative.parts or any(part in {"..", "."} for part in relative.parts):
        raise OmniError("Output template must remain inside the download directory")
    current = root
    try:
        for part in relative.parts[:-1]:
            if not part.strip(" .") or part.rstrip(" .") != part or _RESERVED.match(part):
                raise OmniError("Invalid output directory name")
            current = current / part
            if current.is_symlink():
                raise OmniError("Refusing a symbolic-link output directory")
            current.mkdir(exist_ok=True)
        target = current / relative.name
        if len(relative.name.encode()) > 240:
            raise OmniError("Output filename is too long; use a shorter template")
        if relative.name.rstrip(" .") != relative.name or _RESERVED.match(relative.name):
            raise OmniError("Invalid output filename")
        for path in [target, Path(str(target) + ".part"), Path(str(target) + ".part.json"),
                     Path(str(target) + ".omni.json"), Path(str(target) + ".omni.lock")]:
            _check_regular(path)
        if not target.resolve().is_relative_to(root):
            raise OmniError("Output template escapes the download directory")
        return target
    except OSError as exc:
        raise OmniError("Cannot prepare output path") from exc


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    _check_regular(path)
    try:
        if path.stat().st_size > 16384:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError, UnicodeError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    _check_regular(path)
    fd, name = tempfile.mkstemp(prefix=".omni-", suffix=".json.tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def check_existing(media: Media, path: Path, overwrite: bool = False, *, variant: str | None = None) -> DownloadResult | None:
    """Skip only files with a matching source identity and verified content."""
    _check_regular(path)
    if not path.exists() or overwrite:
        return None
    marker = _read_json(Path(str(path) + ".omni.json"))
    if marker.get("source_hash") != _source_hash(media) or marker.get("variant") != variant:
        raise OmniError("Output already exists without a matching completion record; choose another name or --force-overwrites")
    digest = file_digest(path)
    size = path.stat().st_size
    if marker.get("sha256") != digest or marker.get("size") != size:
        raise OmniError("Existing output failed its integrity check; use --force-overwrites to replace it")
    return DownloadResult(media, str(path), size, digest, skipped=True)


def record_complete(media: Media, path: Path, *, variant: str | None = None) -> DownloadResult:
    _check_regular(path)
    size = path.stat().st_size
    digest = file_digest(path)
    marker = {
        "version": 1, "source_hash": _source_hash(media), "sha256": digest, "size": size,
    }
    if variant is not None:
        marker["variant"] = variant
    _write_json(Path(str(path) + ".omni.json"), marker)
    return DownloadResult(media, str(path), size, digest)


@contextlib.contextmanager
def _destination_lock(path: Path):
    """Persistent advisory lock; the OS releases it even if a process crashes."""
    lockpath = Path(str(path) + ".omni.lock")
    _check_regular(lockpath)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lockpath, flags, 0o600)
    try:
        if os.name == "nt":
            import msvcrt
            if not os.fstat(fd).st_size:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise OmniError("Another download is using this output path") from exc
        else:
            import fcntl
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise OmniError("Another download is using this output path") from exc
        yield
    finally:
        os.close(fd)


def _retry_delay(attempt: int, headers=None) -> float:
    value = headers.get("Retry-After", "") if headers else ""
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            delay = min(0.5 * 2 ** min(attempt, 6), 15)
    if not math.isfinite(delay):
        delay = min(0.5 * 2 ** min(attempt, 6), 15)
    return min(max(delay, 0), 30)


def _partial_state(media: Media, part: Path, statepath: Path, options: TransferOptions) -> dict:
    state = _read_json(statepath)
    if not part.exists() or not options.resume or state.get("source_hash") != _source_hash(media):
        return {}
    validator = state.get("etag") or state.get("last_modified")
    if not isinstance(validator, str) or not validator or any(ord(c) < 32 for c in validator):
        return {}
    if state.get("size") != part.stat().st_size or state.get("sha256") != file_digest(part):
        return {}
    return state


def _save_partial(part: Path, statepath: Path, state: dict) -> None:
    if part.exists() and state:
        state = dict(state, size=part.stat().st_size, sha256=file_digest(part))
        _write_json(statepath, state)


def _transfer(media: Media, path: Path, options: TransferOptions) -> DownloadResult:
    existing = check_existing(media, path, options.overwrite)
    if existing:
        return existing
    part = Path(str(path) + ".part")
    statepath = Path(str(path) + ".part.json")
    if part.exists() and _read_json(statepath).get("source_hash") != _source_hash(media):
        raise OmniError("Partial output belongs to an unknown source; choose another filename or remove the partial")
    opener = build_opener(options.cookies)
    supplied_headers = options.headers
    if media.source_url and _origin(media.source_url) != _origin(media.url):
        supplied_headers = {key: value for key, value in supplied_headers.items()
                            if key.lower() in {"user-agent", "accept", "accept-language", "accept-encoding"}}
    headers = request_headers({**media.headers, **supplied_headers})
    source_hash = _source_hash(media)
    last_error = "Transfer failed"
    for attempt in range(options.retries + 1):
        state = _partial_state(media, part, statepath, options)
        offset = part.stat().st_size if state else 0
        attempt_headers = dict(headers)
        if offset:
            attempt_headers["Range"] = f"bytes={offset}-"
            attempt_headers["If-Range"] = state.get("etag") or state["last_modified"]
        retry_headers = None
        try:
            with opener.open(Request(request_url(media.url), headers=attempt_headers), timeout=options.timeout) as response:
                validate_url(response.geturl())
                status = response.status
                if status not in {200, 206}:
                    raise OmniError(f"Unexpected HTTP status {status}")
                encoding = response.headers.get("Content-Encoding", "identity").lower()
                if encoding not in {"", "identity"}:
                    raise OmniError("Server ignored identity encoding; refusing ambiguous transfer offsets")
                raw_length = response.headers.get("Content-Length")
                try:
                    content_length = int(raw_length) if raw_length is not None else None
                    if content_length is not None and content_length < 0:
                        raise ValueError
                except ValueError as exc:
                    raise OmniError("Server returned an invalid Content-Length") from exc
                total = content_length
                if status == 206:
                    match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("Content-Range", ""))
                    if not match:
                        raise OmniError("Server returned an invalid Content-Range")
                    start, end, total = map(int, match.groups())
                    if start != offset or end < start or end >= total or end != total - 1:
                        raise OmniError("Server returned an unexpected byte range")
                    if content_length is not None and content_length != end - start + 1:
                        raise OmniError("Server returned conflicting range and length headers")
                    for key, header in [("etag", "ETag"), ("last_modified", "Last-Modified")]:
                        if offset and state.get(key) and response.headers.get(header) not in {None, state[key]}:
                            raise OmniError("Resource validator changed during a resumed transfer")
                    if offset and state.get("total") not in {None, total}:
                        raise OmniError("Resource size changed during a resumed transfer")
                elif offset:
                    # Server ignored Range, or If-Range detected a changed resource.
                    offset = 0
                if options.max_filesize is not None and total is not None and total > options.max_filesize:
                    raise OmniError("Media exceeds --max-filesize")
                if total is not None and total - offset > shutil.disk_usage(path.parent).free:
                    raise OmniError("Insufficient free disk space for this download")
                etag = response.headers.get("ETag", "")
                if etag.startswith("W/"):
                    etag = ""
                state = {
                    "version": 1, "source_hash": source_hash,
                    "etag": etag, "last_modified": response.headers.get("Last-Modified", ""), "total": total,
                }
                # Identify ownership before creating/truncating partial bytes. A
                # crash here safely forces a full restart on the next invocation.
                _write_json(statepath, state)
                flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
                flags |= os.O_APPEND if offset else os.O_TRUNC
                fd = os.open(part, flags, 0o600)
                received = 0
                started = time.monotonic()
                try:
                    with os.fdopen(fd, "ab" if offset else "wb") as handle:
                        while True:
                            try:
                                chunk = response.read(min(_CHUNK, options.rate_limit or _CHUNK))
                            except http.client.IncompleteRead as exc:
                                chunk = exc.partial
                                if chunk:
                                    if options.max_filesize is not None and offset + received + len(chunk) > options.max_filesize:
                                        raise OmniError("Media exceeds --max-filesize") from exc
                                    handle.write(chunk)
                                    received += len(chunk)
                                raise
                            if not chunk:
                                break
                            if options.max_filesize is not None and offset + received + len(chunk) > options.max_filesize:
                                raise OmniError("Media exceeds --max-filesize")
                            handle.write(chunk)
                            received += len(chunk)
                            if options.rate_limit:
                                wait = received / options.rate_limit - (time.monotonic() - started)
                                if wait > 0:
                                    time.sleep(wait)
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    _save_partial(part, statepath, state)
                if total is not None and offset + received != total:
                    raise http.client.IncompleteRead(b"", total - offset - received)
                if options.overwrite:
                    _check_regular(path)
                    os.replace(part, path)
                else:
                    try:
                        os.link(part, path)
                    except FileExistsError as exc:
                        raise OmniError("Output appeared during transfer; refusing to overwrite it") from exc
                    part.unlink()
                statepath.unlink(missing_ok=True)
                return record_complete(media, path)
        except HTTPError as exc:
            retry_headers = exc.headers
            if exc.code == 416 and offset:
                # A full GET is safer than treating an unverified partial as complete.
                statepath.unlink(missing_ok=True)
                last_error = "Server rejected resume range"
            elif exc.code in {408, 425, 429} or 500 <= exc.code <= 599:
                last_error = f"HTTP {exc.code}"
            else:
                exc.close()
                raise OmniError(f"Download failed: HTTP {exc.code}") from exc
            exc.close()
        except (URLError, TimeoutError, socket.timeout, ConnectionError, http.client.HTTPException) as exc:
            last_error = type(exc).__name__
        if attempt < options.retries:
            time.sleep(_retry_delay(attempt, retry_headers))
    raise OmniError(f"Download failed after {options.retries + 1} attempt(s): {last_error}; partial data retained")


def download(media: Media, options: TransferOptions | None = None) -> DownloadResult:
    """Download one HTTP media object, preserving original bytes and resumability."""
    options = options or TransferOptions()
    validate_url(media.url)
    if (not isinstance(options.retries, int) or options.retries < 0
            or not isinstance(options.timeout, (int, float))
            or not math.isfinite(options.timeout) or options.timeout <= 0):
        raise OmniError("Retries must be nonnegative and timeout must be positive")
    if options.rate_limit is not None and (not isinstance(options.rate_limit, int) or options.rate_limit <= 0):
        raise OmniError("Rate limit must be positive")
    if options.max_filesize is not None and (not isinstance(options.max_filesize, int) or options.max_filesize < 0):
        raise OmniError("Maximum filesize must be nonnegative")
    path = output_path(media, options)
    try:
        with _destination_lock(path):
            return _transfer(media, path, options)
    except OSError as exc:
        raise OmniError("Cannot read or write download files") from exc
