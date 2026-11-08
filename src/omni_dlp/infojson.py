"""Owned, verified info JSON sidecars with atomic publication."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path

from .models import OmniError
from .transfer import _check_regular, _destination_lock

_MAX_BYTES = 16 * 1024 * 1024
_MAX_RECEIPT_BYTES = 4096
_MAX_DEPTH = 64
_MAX_ITEMS = 250_000
_HEX_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REPLACE_HINT = "choose another name or use --force-overwrites"


def _validate_tree(value: object) -> None:
    """Bound recursive work and reject non-JSON values without coercing keys."""
    active: set[int] = set()
    pending = [(value, 0, False)]
    count = 0
    while pending:
        item, depth, leaving = pending.pop()
        if leaving:
            active.remove(id(item))
            continue
        count += 1
        if count > _MAX_ITEMS or depth > _MAX_DEPTH:
            raise OmniError("Info JSON contains too many items or exceeds the nesting limit")
        if isinstance(item, (dict, list)):
            if id(item) in active:
                raise OmniError("Info JSON contains a circular reference")
            if len(item) > _MAX_ITEMS:
                raise OmniError("Info JSON contains too many items")
            active.add(id(item))
            pending.append((item, depth, True))
            if isinstance(item, dict):
                for key in item:
                    if not isinstance(key, str) or len(key) > _MAX_BYTES:
                        raise OmniError("Info JSON requires bounded string keys")
                pending.extend((child, depth + 1, False) for child in item.values())
            else:
                pending.extend((child, depth + 1, False) for child in item)
        elif isinstance(item, str):
            if len(item) > _MAX_BYTES:
                raise OmniError("Info JSON exceeds the 16 MiB size limit")
        elif isinstance(item, float):
            if not math.isfinite(item):
                raise OmniError("Info JSON cannot contain nonfinite numbers")
        elif item is not None and not isinstance(item, (int, bool)):
            raise OmniError("Info JSON contains an unsupported value type")


def _encode(record: dict) -> bytes:
    if not isinstance(record, dict):
        raise OmniError("Info JSON requires an object record")
    _validate_tree(record)
    chunks: list[bytes] = []
    total = 1
    try:
        encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        for chunk in encoder.iterencode(record):
            encoded = chunk.encode("utf-8")
            total += len(encoded)
            if total > _MAX_BYTES:
                raise OmniError("Info JSON exceeds the 16 MiB size limit")
            chunks.append(encoded)
    except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as exc:
        raise OmniError("Info JSON cannot be serialized as valid UTF-8 JSON") from exc
    return b"".join(chunks) + b"\n"


def _identity(record: dict) -> str:
    media = record.get("media")
    if not isinstance(media, dict):
        raise OmniError("Info JSON requires a media record with a stable identity")
    metadata = media.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise OmniError("Info JSON media metadata must be an object")
    # Archive identity survives format selection and metadata-only runs. A
    # prepared resource_id may include a format suffix unavailable before that.
    candidates = (("archive", metadata.get("archive_id")),
                  ("resource", metadata.get("resource_id")),
                  ("source", media.get("source_url")), ("url", media.get("url")))
    for kind, value in candidates:
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip():
            raise OmniError("Info JSON media identity must be a nonempty string")
        return hashlib.sha256(("info-json-v1\0" + kind + "\0" + value).encode("utf-8")).hexdigest()
    raise OmniError("Info JSON requires a stable media identity")


def _read(path: Path, limit: int) -> bytes | None:
    """Read a regular file with bounded allocation, without following a symlink."""
    _check_regular(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as source:
        details = os.fstat(source.fileno())
        if not stat.S_ISREG(details.st_mode):
            raise OmniError("Info JSON or its receipt is not a regular file")
        if details.st_size > limit:
            raise OmniError("Existing info JSON or its receipt exceeds the safe size limit")
        content = source.read(limit + 1)
    _check_regular(path)
    if len(content) > limit:
        raise OmniError("Existing info JSON or its receipt exceeds the safe size limit")
    return content


def _parse(content: bytes) -> dict:
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def invalid_number(value):
        raise ValueError("nonfinite number")

    try:
        parsed = json.loads(content.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid_number)
        if not isinstance(parsed, dict):
            raise ValueError("non-object JSON")
        _validate_tree(parsed)
        return parsed
    except (ValueError, UnicodeError, RecursionError, OverflowError, OmniError) as exc:
        raise OmniError(f"Existing info JSON or receipt is invalid; {_REPLACE_HINT}") from exc


def _prepare(path: Path) -> Path:
    path = Path(os.path.abspath(path))
    if len((path.name + ".omni.json").encode("utf-8")) > 255:
        raise OmniError("Info JSON filename is too long for its receipt; shorten the output template")
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        if current.is_symlink():
            raise OmniError("Refusing a symbolic-link info JSON directory")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise OmniError("Info JSON parent is not a directory")
    return path


def _temporary(path: Path, content: bytes) -> Path:
    fd, name = tempfile.mkstemp(prefix=".omni-info-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return temporary
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _publish(temporary: Path, destination: Path, replace: bool) -> None:
    _check_regular(destination)
    if replace:
        os.replace(temporary, destination)
    elif os.name == "nt":
        # Windows rename refuses an existing destination, without requiring
        # hard-link support on removable filesystems.
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()


def _owned(content: bytes | None, receipt: bytes | None, identity: str) -> bool:
    if receipt is None:
        if content is not None:
            raise OmniError(f"Info JSON already exists without an ownership receipt; {_REPLACE_HINT}")
        return False
    marker = _parse(receipt)
    if (set(marker) != {"version", "kind", "identity_sha256", "sha256", "size"}
            or type(marker.get("version")) is not int or marker["version"] != 1
            or marker.get("kind") != "omni-info-json"
            or not isinstance(marker.get("identity_sha256"), str)
            or not _HEX_DIGEST.fullmatch(marker["identity_sha256"])
            or not isinstance(marker.get("sha256"), str) or not _HEX_DIGEST.fullmatch(marker["sha256"])
            or type(marker.get("size")) is not int or not 0 < marker["size"] <= _MAX_BYTES):
        raise OmniError(f"Info JSON has an invalid ownership receipt; {_REPLACE_HINT}")
    if marker["identity_sha256"] != identity:
        raise OmniError(f"Info JSON belongs to different media; {_REPLACE_HINT}")
    if content is None:
        return True
    if len(content) != marker["size"] or hashlib.sha256(content).hexdigest() != marker["sha256"]:
        raise OmniError(f"Existing info JSON failed its integrity check; {_REPLACE_HINT}")
    if _identity(_parse(content)) != identity:
        raise OmniError(f"Info JSON ownership does not match its media; {_REPLACE_HINT}")
    return True


def write_info_json(path: Path, record: dict, *, overwrite: bool = False) -> None:
    """Write a redacted public record; refresh only verified, same-media sidecars.

    The caller must redact private metadata before calling this function.
    Identity and content are hashed in the receipt, never copied into it.
    """
    content = _encode(record)
    identity = _identity(record)
    marker = _encode({"version": 1, "kind": "omni-info-json", "identity_sha256": identity,
                      "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)})
    temporaries: list[Path] = []
    try:
        path = _prepare(Path(path))
        receipt_path = Path(str(path) + ".omni.json")
        with _destination_lock(path):
            _check_regular(path)
            _check_regular(receipt_path)
            try:
                previous = _read(path, _MAX_BYTES)
                previous_receipt = _read(receipt_path, _MAX_RECEIPT_BYTES)
                owned = _owned(previous, previous_receipt, identity)
            except OmniError:
                if not overwrite:
                    raise
                # Explicit replacement can discard an oversized, invalid, or
                # unrelated regular file without reading unbounded content.
                previous = previous_receipt = None
                owned = False
            if owned and previous == content:
                return
            temporaries.append(_temporary(path, content))
            temporaries.append(_temporary(receipt_path, marker))
            # Catch edits by cooperating tools and ordinary editors while the
            # temporary files were prepared. Advisory locks are not a security
            # boundary against a process actively racing filesystem operations.
            _prepare(path)
            if not overwrite and (_read(path, _MAX_BYTES) != previous
                                  or _read(receipt_path, _MAX_RECEIPT_BYTES) != previous_receipt):
                raise OmniError("Info JSON or its receipt changed during the write; retry without editing it")
            _check_regular(receipt_path)
            _publish(temporaries[0], path, overwrite or owned and previous is not None)
            _publish(temporaries[1], receipt_path, overwrite or previous_receipt is not None)
    except (OSError, ValueError, UnicodeError) as exc:
        raise OmniError("Cannot safely publish info JSON and its receipt; check paths and filesystem permissions") from exc
    finally:
        for temporary in temporaries:
            temporary.unlink(missing_ok=True)
