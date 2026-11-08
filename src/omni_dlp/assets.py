"""Plan and transfer media sidecars without weakening the native file safeguards."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit, urlunsplit

from .models import DownloadResult, Media, OmniError
from .transfer import (
    OUTPUT_SIDECARS, TransferOptions, _check_cancel, _check_regular, _destination_lock, _origin,
    check_existing, download, output_path, record_complete, request_headers, validate_url,
)

_MAX_LANGUAGES = 256
_MAX_TRACKS = 64
_MAX_THUMBNAILS = 512
_MAX_TEXT_BYTES = 8 * 1024 * 1024
_SAFE_HEADERS = {"user-agent", "accept", "accept-language", "accept-encoding"}
_SIDECARS = OUTPUT_SIDECARS


@dataclass
class AssetOptions:
    write_subs: bool = False
    write_auto_subs: bool = False
    sub_langs: str | None = None
    sub_format: str = "vtt/srt/best"
    write_thumbnail: bool = False
    write_all_thumbnails: bool = False
    write_description: bool = False


@dataclass
class AssetPlan:
    media: Media
    path: Path
    inline_text: str | None = None
    kind: str = "subtitle"
    language: str = ""
    main_path: Path | None = field(default=None, repr=False)


def _pattern(pattern: str, *, glob: bool = False):
    """Compile a bounded regular-language subset into a linear token sequence.

    Matching uses dynamic programming, so even repeated stars do not introduce
    catastrophic regex backtracking. Groups, backreferences and counted repeats
    are rejected rather than interpreted by Python's regular-expression engine.
    """
    if not pattern or len(pattern) > 128:
        raise OmniError("Subtitle language patterns must contain 1 to 128 characters")
    pattern = pattern.casefold()
    if not glob:
        if pattern.startswith("^"):
            pattern = pattern[1:]
        if pattern.endswith("$") and not pattern.endswith(r"\$"):
            pattern = pattern[:-1]
    tokens = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        index += 1
        if char == "\\":
            if index >= len(pattern) or pattern[index].isalnum():
                raise OmniError("Subtitle language patterns do not support escape classes or backreferences")
            matcher = ("literal", pattern[index])
            index += 1
        elif char == "[":
            negated = index < len(pattern) and pattern[index] in ({"^", "!"} if glob else {"^"})
            if negated:
                index += 1
            members = []
            while index < len(pattern) and pattern[index] != "]":
                start = pattern[index]
                if start in {"[", "\\"}:
                    raise OmniError("Unsupported subtitle language character class")
                index += 1
                if index + 1 < len(pattern) and pattern[index] == "-" and pattern[index + 1] != "]":
                    end = pattern[index + 1]
                    if ord(start) > ord(end):
                        raise OmniError("Invalid subtitle language character range")
                    index += 2
                else:
                    end = start
                members.append((ord(start), ord(end)))
            if not members or index >= len(pattern):
                raise OmniError("Invalid subtitle language character class")
            index += 1
            matcher = ("class", (negated, members))
        elif (glob and char in {"?", "*"}) or (not glob and char == "."):
            matcher = ("any", None)
        elif not glob and char in "()*+?{}|^$]":
            raise OmniError("Unsupported subtitle language regex syntax; use literals, dots, classes and simple quantifiers")
        else:
            matcher = ("literal", char)
        quantifier = "*" if glob and char == "*" else ""
        if not glob and index < len(pattern) and pattern[index] in "*+?":
            quantifier = pattern[index]
            index += 1
        tokens.append((matcher, quantifier))
    return tokens


def _matches(tokens, language: str) -> bool:
    text = language.casefold()
    previous = [True] + [False] * len(text)
    for (kind, value), quantifier in tokens:
        current = [previous[0] if quantifier in {"*", "?"} else False] + [False] * len(text)
        for index, char in enumerate(text, 1):
            if kind == "any":
                matched = char != "\n"
            elif kind == "literal":
                matched = char == value
            else:
                negated, ranges = value
                matched = any(low <= ord(char) <= high for low, high in ranges) != negated
            if quantifier == "*":
                current[index] = previous[index] or (matched and current[index - 1])
            elif quantifier == "+":
                current[index] = matched and (previous[index - 1] or current[index - 1])
            elif quantifier == "?":
                current[index] = previous[index] or (matched and previous[index - 1])
            else:
                current[index] = matched and previous[index - 1]
        previous = current
    return previous[-1]


def _languages(available: dict, manual: set[str], query: str | None) -> list[str]:
    keys = list(available)
    if query is None:
        candidates = (["en"] if "en" in manual else [])
        candidates += [key for key in keys if key in manual and key.startswith("en")]
        candidates += ["en"] if "en" in available else []
        candidates += [key for key in keys if key.startswith("en")]
        candidates += [key for key in keys if key in manual] + keys
        return candidates[:1]
    if not isinstance(query, str) or len(query) > 2048:
        raise OmniError("Subtitle language selection must be a bounded comma-separated string")
    expressions = query.split(",")
    if len(expressions) > 64:
        raise OmniError("Too many subtitle language expressions")
    selected = []
    work = 0
    for expression in expressions:
        expression = expression.strip()
        exclude = expression.startswith("-")
        expression = expression[1:] if exclude else expression
        if expression == "all":
            matching = keys
        else:
            glob = expression.startswith("glob:")
            expression = expression[5:] if glob else expression.removeprefix("re:")
            tokens = _pattern(expression, glob=glob)
            work += len(tokens) * sum(len(key.casefold()) for key in keys)
            if work > 2_000_000:
                raise OmniError("Subtitle language patterns exceed the matching work limit")
            matching = [key for key in keys if _matches(tokens, key)]
        if exclude:
            selected = [key for key in selected if key not in matching]
        else:
            selected.extend(key for key in matching if key not in selected)
    return selected


def _subtitle_map(metadata: dict, name: str) -> dict[str, list[dict]]:
    value = metadata.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > _MAX_LANGUAGES:
        raise OmniError("Subtitle metadata must be a bounded language-to-tracks mapping")
    output = {}
    for language, tracks in value.items():
        if (not isinstance(language, str) or not language or len(language) > 128
                or any(ord(char) < 32 or ord(char) == 127 for char in language)):
            raise OmniError("Invalid subtitle language metadata")
        if not isinstance(tracks, list) or len(tracks) > _MAX_TRACKS:
            raise OmniError("Subtitle tracks must be a bounded list")
        if not all(isinstance(track, dict) for track in tracks):
            raise OmniError("Invalid subtitle track metadata")
        if tracks:
            output[language] = tracks
    return output


def _url(track: dict, kind: str) -> str:
    value = track.get("url")
    if not isinstance(value, str) or not value:
        raise OmniError(f"Selected {kind} has no HTTP(S) URL")
    try:
        validate_url(value)
    except OmniError as exc:
        raise OmniError(f"Selected {kind} has an invalid HTTP(S) URL") from exc
    return value


def _extension(track: dict, url: str, *, thumbnail: bool = False) -> str:
    extension = track.get("ext")
    if extension is None:
        suffix = Path(urlsplit(url).path).suffix.lstrip(".").lower()
        extension = suffix if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,15}", suffix) else "jpg" if thumbnail else "bin"
    if not isinstance(extension, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,15}", extension):
        raise OmniError("Invalid asset filename extension")
    return extension.lower()


def _token(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", value)
    safe = re.sub(r"\.{2,}", "_", safe).strip(" .")[:64] or "asset"
    if safe != value:
        safe += "-" + hashlib.sha256(value.encode()).hexdigest()[:8]
    return safe


def _related(path: Path) -> set[str]:
    return {str(path.absolute()).casefold() + suffix for suffix in _SIDECARS}


def _headers(media: Media, track: dict, target: str) -> dict[str, str]:
    inherited = request_headers(media.headers)
    source = media.source_url or media.url
    # Main-media headers may already target a CDN. Only inherit credentials when
    # the page, media and asset all share an origin. Explicit asset headers belong
    # to this asset; global user headers are separately scoped by transfer.py.
    if _origin(target) != _origin(source) or _origin(target) != _origin(media.url):
        inherited = {key: value for key, value in inherited.items() if key.lower() in _SAFE_HEADERS}
    explicit = track.get("headers", track.get("http_headers", {}))
    if not isinstance(explicit, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in explicit.items()):
        raise OmniError("Asset request headers must be string pairs")
    return request_headers({**inherited, **explicit})


def _asset(media: Media, track: dict, kind: str, language: str = "", *, identifier: str = "") -> Media:
    url = _url(track, kind)
    extension = _extension(track, url, thumbnail=kind == "thumbnail")
    parsed = urlsplit(url)
    identity_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    base = str(media.metadata.get("archive_id") or media.metadata.get("resource_id") or media.source_url or media.url)
    resource = hashlib.sha256("\0".join((base, kind, language, extension, identifier, identity_url)).encode()).hexdigest()
    return Media(url=url, title=media.title, id=f"{media.id}-{kind}-{_token(identifier or language or 'asset')}",
                 kind="image" if kind == "thumbnail" else "subtitle", ext=extension,
                 source_url=media.source_url or media.url, headers=_headers(media, track, url),
                 metadata={"asset_kind": kind, "language": language, "resource_id": "asset:" + resource})


def _preferences(value: str) -> list[str]:
    if not isinstance(value, str) or len(value) > 256:
        raise OmniError("Subtitle format preference must be a bounded slash-separated string")
    parts = (value or "best").lower().split("/")
    if len(parts) > 32 or any(not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,15}", part) for part in parts):
        raise OmniError("Invalid subtitle format preference")
    return parts


def plan_assets(media: Media, main_path: Path, opts: AssetOptions) -> list[AssetPlan]:
    """Plan sidecars beside the final media path, without creating files."""
    for name in ("write_subs", "write_auto_subs", "write_thumbnail", "write_all_thumbnails", "write_description"):
        if not isinstance(getattr(opts, name), bool):
            raise OmniError("Asset enable flags must be booleans")
    main_path = Path(main_path)
    if (not main_path.name or any(part == ".." for part in main_path.parts)
            or re.search(r'[<>:"/\\|?*\x00-\x1f\x7f]', main_path.name)
            or main_path.name.rstrip(" .") != main_path.name):
        raise OmniError("Invalid main media path for asset planning")
    metadata = media.metadata
    if not isinstance(metadata, dict):
        raise OmniError("Invalid media metadata for assets")
    base = main_path.parent / main_path.stem
    occupied = _related(main_path)
    plans = []

    def add(asset: Media, path: Path, kind: str, language: str = "", text: str | None = None):
        if len(path.name.encode()) > 240:
            raise OmniError("Asset filename is too long; use a shorter media output template")
        related = _related(path)
        if occupied.intersection(related):
            raise OmniError("Asset outputs overlap media or another asset; use distinct language or thumbnail identifiers")
        occupied.update(related)
        plans.append(AssetPlan(asset, path, text, kind, language, main_path))

    if opts.write_subs or opts.write_auto_subs:
        manual = _subtitle_map(metadata, "subtitles") if opts.write_subs else {}
        available = dict(manual)
        if opts.write_auto_subs:
            for key, tracks in _subtitle_map(metadata, "automatic_captions").items():
                available.setdefault(key, tracks)
        if len(available) > _MAX_LANGUAGES:
            raise OmniError("Too many combined subtitle languages")
        preferences = _preferences(opts.sub_format)
        for language in _languages(available, set(manual), opts.sub_langs):
            tracks = available[language]
            candidates = [(track, _url(track, "subtitle")) for track in tracks]
            extensions = [_extension(track, url) for track, url in candidates]
            selected = None
            for preference in preferences:
                matches = list(range(len(tracks))) if preference == "best" else [i for i, ext in enumerate(extensions) if ext == preference]
                if matches:
                    selected = matches[-1]
                    break
            fallback = selected is None
            selected = len(tracks) - 1 if selected is None else selected
            asset = _asset(media, tracks[selected], "subtitle", language,
                           identifier=("manual-" if language in manual else "automatic-") + language)
            asset.metadata.update(automatic=language not in manual, format_fallback=fallback)
            add(asset, base.with_name(base.name + f".{_token(language)}.{asset.ext}"), "subtitle", language)

    if opts.write_thumbnail or opts.write_all_thumbnails:
        thumbnails = metadata.get("thumbnails")
        if thumbnails is None:
            thumbnails = []
        if not isinstance(thumbnails, list) or len(thumbnails) > _MAX_THUMBNAILS:
            raise OmniError("Thumbnail metadata must be a bounded list")
        candidates = []
        seen = set()
        for index, thumbnail in enumerate(thumbnails):
            if not isinstance(thumbnail, dict):
                raise OmniError("Invalid thumbnail metadata")
            url = _url(thumbnail, "thumbnail")
            if url in seen:
                continue
            seen.add(url)
            candidates.append((index, thumbnail))

        def rank(entry):
            index, thumbnail = entry
            values = []
            for key in ("width", "height"):
                value = thumbnail.get(key)
                values.append(float(value) if isinstance(value, (int, float)) and not isinstance(value, bool)
                              and math.isfinite(value) and value >= 0 else 0)
            return values[0] * values[1], values[0], values[1], index

        if candidates and not opts.write_all_thumbnails:
            candidates = [max(candidates, key=rank)]
        multiple = len(candidates) > 1
        for index, thumbnail in candidates:
            value = thumbnail.get("id", str(index))
            if not isinstance(value, (str, int)) or isinstance(value, bool):
                raise OmniError("Invalid thumbnail identifier")
            identifier = str(value)
            if len(identifier) > 256:
                raise OmniError("Thumbnail identifier exceeds the length limit")
            asset = _asset(media, thumbnail, "thumbnail", identifier=identifier)
            suffix = f".{_token(identifier)}.{asset.ext}" if multiple else f".{asset.ext}"
            path = base.with_name(base.name + suffix)
            if str(path.absolute()).casefold() == str(main_path.absolute()).casefold():
                path = base.with_name(base.name + ".thumbnail" + suffix)
            add(asset, path, "thumbnail")

    if opts.write_description and metadata.get("description") is not None:
        description = metadata["description"]
        if not isinstance(description, str):
            raise OmniError("Media description must be text")
        if len(description) > _MAX_TEXT_BYTES:
            raise OmniError("Description exceeds the 8 MiB inline text limit")
        try:
            encoded = description.encode("utf-8")
        except UnicodeError as exc:
            raise OmniError("Media description contains invalid Unicode") from exc
        if len(encoded) > _MAX_TEXT_BYTES:
            raise OmniError("Description exceeds the 8 MiB inline text limit")
        asset = Media(url=media.source_url or media.url, title=media.title, id=media.id + "-description", kind="document", ext="description",
                      source_url=media.source_url or media.url, metadata={"asset_kind": "description", "resource_id":
                      "description:" + hashlib.sha256(str(media.metadata.get("archive_id") or media.metadata.get("resource_id") or media.url).encode()).hexdigest()})
        path = base.with_name(base.name + ".description")
        if str(path.absolute()).casefold() == str(main_path.absolute()).casefold():
            path = base.with_name(base.name + ".metadata.description")
        add(asset, path, "description", text=description)
    return plans


def _prepared(plans: list[AssetPlan], options: TransferOptions) -> list[tuple[AssetPlan, TransferOptions]]:
    if len(plans) > _MAX_LANGUAGES + _MAX_THUMBNAILS + 1:
        raise OmniError("Too many asset plans")
    root_input = Path(options.directory).expanduser().absolute()
    root = root_input.resolve()

    def canonical(path: Path) -> Path:
        path = path.absolute()
        for base in (root_input, root):
            try:
                return root / path.relative_to(base)
            except ValueError:
                continue
        raise OmniError("Asset output escapes the download directory")

    occupied = set()
    for plan in plans:
        if plan.main_path is not None:
            occupied.update(_related(canonical(Path(plan.main_path))))
    prepared = []
    for plan in plans:
        if plan.kind not in {"subtitle", "thumbnail", "description"}:
            raise OmniError("Unknown asset plan kind")
        path = Path(plan.path)
        if any(part == ".." for part in path.parts):
            raise OmniError("Asset output escapes the download directory")
        path = canonical(path)
        related = _related(path)
        if occupied.intersection(related):
            raise OmniError("Asset outputs overlap media or another asset")
        occupied.update(related)
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise OmniError("Asset output escapes the download directory") from exc
        asset_options = replace(options, output=relative.as_posix().replace("%", "%%"))
        target = output_path(plan.media, asset_options)
        if target != path:
            raise OmniError("Asset output changed during path validation")
        prepared.append((replace(plan, path=target), asset_options))
    return prepared


def _inline(plan: AssetPlan, options: TransferOptions) -> DownloadResult:
    try:
        if not isinstance(plan.inline_text, str):
            raise OmniError("Inline asset content must be text")
        if len(plan.inline_text) > _MAX_TEXT_BYTES:
            raise OmniError("Inline asset exceeds the text size limit")
        data = plan.inline_text.encode("utf-8")
    except UnicodeError as exc:
        raise OmniError("Inline asset contains invalid Unicode") from exc
    if options.max_filesize is not None and (not isinstance(options.max_filesize, int) or options.max_filesize < 0):
        raise OmniError("Maximum filesize must be nonnegative")
    if len(data) > _MAX_TEXT_BYTES or options.max_filesize is not None and len(data) > options.max_filesize:
        raise OmniError("Inline asset exceeds the configured size limit")
    variant = "inline-text:" + hashlib.sha256(data).hexdigest()
    with _destination_lock(plan.path):
        _check_cancel(options)
        existing = check_existing(plan.media, plan.path, options.overwrite, variant=variant)
        if existing is not None:
            return existing
        descriptor, name = tempfile.mkstemp(prefix=".omni-asset-", dir=plan.path.parent)
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            _check_cancel(options)
            if options.overwrite:
                _check_regular(plan.path)
                os.replace(temporary, plan.path)
            else:
                try:
                    os.link(temporary, plan.path)
                except FileExistsError as exc:
                    raise OmniError("Asset output appeared during writing; refusing to overwrite it") from exc
                temporary.unlink()
            return record_complete(plan.media, plan.path, variant=variant)
        finally:
            temporary.unlink(missing_ok=True)


def download_assets(plans: list[AssetPlan], transfer_options: TransferOptions) -> list[DownloadResult]:
    """Transfer planned sidecars with the native retry, integrity and cancel rules."""
    _check_cancel(transfer_options)
    prepared = _prepared(plans, transfer_options)
    results = []
    for plan, options in prepared:
        _check_cancel(options)
        try:
            result = _inline(plan, options) if plan.inline_text is not None else download(plan.media, options)
        except OSError as exc:
            raise OmniError("Cannot write media asset files") from exc
        results.append(result)
    return results
