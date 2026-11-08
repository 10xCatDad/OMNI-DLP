"""Public SoundCloud tracks and sets from advertised website playback data.

No account login, private-link tokens, JavaScript execution, or upstream runtime
is used. Website playback endpoints are distinct from the registered OAuth API.
"""
from __future__ import annotations

import hashlib
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

from ..models import Format, Media, OmniError
from ..transfer import validate_url
from ..types import classify
from .common import ExtractorContext

_PAGE_HOSTS = {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"}
_RESERVED = {"discover", "stream", "you", "search", "charts", "upload", "settings", "terms", "pages"}
_API = "https://api-v2.soundcloud.com/"
_CLIENT = re.compile(r"[A-Za-z0-9]{16,128}\Z")
_MAX_SCRIPTS = 6
_MAX_TRANSCODINGS = 16


def _page_url(url: str, *, short: bool = True) -> str:
    validate_url(url)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise OmniError("SoundCloud URLs must use their standard HTTP(S) port")
    if any(key.lower() in {"secret_token", "oauth_token", "access_token"} for key, _ in parse_qsl(parsed.query)):
        raise OmniError("Private SoundCloud links and account tokens are not supported")
    parts = [part for part in parsed.path.split("/") if part]
    if any("/" in unquote(part) or "\\" in unquote(part) or unquote(part) in {".", ".."} for part in parts):
        raise OmniError("SoundCloud URLs contain an invalid path component")
    if short and host == "on.soundcloud.com" and len(parts) == 1:
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    if host not in _PAGE_HOSTS or not parts or parts[0].lower() in _RESERVED:
        raise OmniError("Use a public SoundCloud track or set URL")
    if not (len(parts) == 2 and parts[1] not in {"sets", "tracks", "albums", "likes", "reposts"}
            or len(parts) == 3 and parts[1] == "sets"):
        raise OmniError("Use a public SoundCloud track or set URL")
    return "https://soundcloud.com/" + "/".join(parts)


def supports(url: str) -> bool:
    try:
        _page_url(url)
        return True
    except (OmniError, TypeError, ValueError):
        return False


def _number(value, *, integer=False):
    try:
        number = float(value) if not isinstance(value, bool) else float("nan")
        if not math.isfinite(number) or number < 0:
            return None
        return int(number) if integer else number
    except (ValueError, TypeError, OverflowError):
        return None


def _playlist_count(data: dict, present: int) -> int:
    value = data.get("track_count")
    if value is None:
        return present
    count = _number(value)
    if count is None or not count.is_integer():
        raise OmniError("SoundCloud returned an invalid playlist track count")
    return max(int(count), present)


def _identifier(value) -> str | None:
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9]\d{0,19}", value):
        return value
    return None


def _cdn(value, *, image=False) -> str:
    if not isinstance(value, str):
        raise OmniError("SoundCloud returned an invalid media URL")
    try:
        url = urljoin("https://soundcloud.com/", value)
    except ValueError as exc:
        raise OmniError("SoundCloud returned an invalid media URL") from exc
    validate_url(url)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    owned = host == "sndcdn.com" or host.endswith(".sndcdn.com")
    if not image:
        owned = owned or host == "soundcloud.cloud" or host.endswith(".soundcloud.cloud")
    if not owned or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise OmniError("SoundCloud returned a media URL outside its supported storage hosts")
    return url


def _hydration(text: str) -> list[dict]:
    match = re.search(r"(?:window\.)?__sc_hydration\s*=\s*", text)
    if not match:
        return []
    try:
        values, _ = json.JSONDecoder().raw_decode(text[match.end():])
    except (ValueError, RecursionError) as exc:
        raise OmniError("SoundCloud returned malformed embedded playback metadata") from exc
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise OmniError("SoundCloud returned invalid embedded playback metadata")
    return values


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.urls = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            value = dict(attrs).get("src")
            if isinstance(value, str):
                self.urls.append(value)


def _client_id(hydration: list[dict], page: str, base: str, context: ExtractorContext) -> str:
    for item in hydration:
        data = item.get("data")
        if item.get("hydratable") == "apiClient" and isinstance(data, dict):
            candidate = data.get("id")
            if isinstance(candidate, str) and _CLIENT.fullmatch(candidate):
                return candidate
    inline = re.search(r'["\']?client_id["\']?\s*[:=]\s*["\']([A-Za-z0-9]{16,128})["\']', page)
    if inline:
        return inline[1]
    scripts = _Scripts()
    scripts.feed(page)
    checked = set()
    for value in reversed(scripts.urls):
        try:
            candidate = urljoin(base, value)
            validate_url(candidate)
            parsed = urlsplit(candidate)
        except (ValueError, OmniError):
            continue
        if parsed.hostname != "a-v2.sndcdn.com" or not parsed.path.startswith("/assets/") or not parsed.path.endswith(".js"):
            continue
        if candidate in checked:
            continue
        checked.add(candidate)
        if len(checked) > _MAX_SCRIPTS:
            break
        try:
            raw, final, _ = context.get_document(candidate)
            if urlsplit(final).hostname != "a-v2.sndcdn.com":
                continue
        except OmniError:
            continue
        match = re.search(r'["\']?client_id["\']?\s*[:=]\s*["\']([A-Za-z0-9]{16,128})["\']', raw.decode("utf-8", "replace"))
        if match:
            return match[1]
    raise OmniError("SoundCloud did not advertise a usable public playback client; native extraction cannot continue")


def _api(endpoint: str, client: str, context: ExtractorContext, **params):
    try:
        url = urljoin(_API, endpoint)
    except ValueError as exc:
        raise OmniError("SoundCloud returned an invalid playback API endpoint") from exc
    validate_url(url)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "api-v2.soundcloud.com" or parsed.port not in {None, 443}:
        raise OmniError("SoundCloud returned an unsupported playback API endpoint")
    if not re.match(r"^/(?:resolve$|tracks(?:/[1-9]\d*)?$|playlists/[1-9]\d*$|media/)", parsed.path):
        raise OmniError("SoundCloud returned an unexpected playback API path")
    query = dict(parse_qsl(parsed.query))
    if any(key.lower() in {"secret_token", "oauth_token", "access_token"} for key in query):
        raise OmniError("Private SoundCloud playback endpoints are not supported")
    query.update(params, client_id=client)
    return context.get_json(urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")))


def _public(data: dict, label="track", *, playback=True) -> None:
    if data.get("sharing") not in (None, "public") or data.get("secret_token"):
        raise OmniError(f"Private SoundCloud {label}s are not supported")
    if not playback:
        return
    if data.get("policy") not in (None, "ALLOW", "MONETIZE"):
        raise OmniError(f"SoundCloud {label} is restricted or preview-only")
    if label == "track" and data.get("streamable") is False:
        raise OmniError("SoundCloud track is not publicly streamable")


def _format_id(protocol: str, preset: str, used: set[str]) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", preset)[:80]
    if safe != preset:
        safe += "-" + hashlib.sha256(preset.encode()).hexdigest()[:10]
    base = "sc-" + protocol + "-" + (safe or "audio")
    result, index = base, 1
    while result in used:
        index += 1
        result = base + "-" + str(index)
    used.add(result)
    return result


def _formats(track: dict, client: str, context: ExtractorContext) -> tuple[list[Format], list[str]]:
    media = track.get("media")
    transcodings = media.get("transcodings") if isinstance(media, dict) else None
    if not isinstance(transcodings, list):
        raise OmniError("SoundCloud track has no advertised public playback formats")
    formats, warnings, seen, used = [], [], set(), set()
    for transcoding in transcodings[:_MAX_TRANSCODINGS]:
        if not isinstance(transcoding, dict):
            warnings.append("Malformed playback format skipped")
            continue
        description = transcoding.get("format")
        description = description if isinstance(description, dict) else {}
        protocol = description.get("protocol")
        if protocol not in ("progressive", "hls"):
            warnings.append("Unsupported or protected playback protocol skipped")
            continue
        endpoint = transcoding.get("url")
        if transcoding.get("snipped") or isinstance(endpoint, str) and "/preview/" in endpoint:
            warnings.append("Preview-only playback format skipped")
            continue
        try:
            response = _api(endpoint, client, context) if isinstance(endpoint, str) else None
            stream = _cdn(response.get("url")) if isinstance(response, dict) else None
            if not stream:
                raise OmniError("SoundCloud playback response has no URL")
            if re.search(r"/(?:preview|playlist)/0/30/", stream):
                warnings.append("Preview-only playback format skipped")
                continue
        except OmniError as exc:
            warnings.append(str(exc))
            continue
        if stream in seen:
            continue
        seen.add(stream)
        preset = transcoding.get("preset")
        preset = preset if isinstance(preset, str) else "audio"
        mime = description.get("mime_type")
        mime = mime if isinstance(mime, str) else ""
        codec_match = re.search(r'codecs\s*=\s*["\']?([^;"\']+)', mime)
        codec = codec_match[1].strip() if codec_match else ""
        _, extension = classify(stream, mime)
        if codec.startswith("mp4a") or mime.startswith("audio/mp4"):
            extension = "m4a"
        elif codec.startswith("opus"):
            extension = "opus"
        elif not extension or extension == "m3u8":
            extension = next((ext for ext in ("mp3", "aac", "opus", "ogg", "flac", "wav") if preset.startswith(ext)), "m3u8" if protocol == "hls" else "bin")
        bitrate = re.search(r"(?:_|-)(\d+)k(?:$|_)", preset)
        abr = float(bitrate[1]) if bitrate else None
        formats.append(Format(url=stream, format_id=_format_id(protocol, preset, used), ext=extension,
                              protocol="hls" if protocol == "hls" else "http", vcodec="none",
                              acodec=codec or ("mp3" if extension == "mp3" else ""), abr=abr, tbr=abr,
                              headers=context.scoped_headers(stream), metadata={"preset": preset,
                                  "mime_type": mime, "quality": transcoding.get("quality", ""), "kind": "audio"}))
    if len(transcodings) > _MAX_TRANSCODINGS:
        warnings.append("Playback format request limit reached")
    return formats, warnings


def _artwork(track: dict) -> str | None:
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    candidate = track.get("artwork_url") or user.get("avatar_url")
    if not candidate:
        return None
    try:
        return _cdn(candidate, image=True)
    except OmniError:
        return None


def _track(track: dict, client: str | None, source: str, context: ExtractorContext) -> list[Media]:
    metadata_only = getattr(context, "metadata_only", False)
    _public(track, playback=not metadata_only)
    identifier = _identifier(track.get("id"))
    if not identifier:
        raise OmniError("SoundCloud returned an invalid track identifier")
    title = track.get("title")
    title = title if isinstance(title, str) and title else identifier
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    artwork = _artwork(track)
    webpage = track.get("permalink_url")
    try:
        webpage = _page_url(webpage, short=False) if isinstance(webpage, str) else source
    except OmniError:
        webpage = source
    duration = _number(track.get("full_duration"))
    if duration is None:
        duration = _number(track.get("duration"))
    metadata = {"extractor": "soundcloud", "archive_id": "soundcloud:" + identifier,
                "webpage_url": webpage, "duration": duration / 1000 if duration is not None else None,
                "uploader": user.get("username") if isinstance(user.get("username"), str) else "",
                "uploader_id": _identifier(user.get("id")),
                "thumbnails": [{"url": artwork}] if artwork else []}
    for key in ("description", "genre", "license", "created_at", "release_date"):
        if isinstance(track.get(key), str):
            metadata[key] = track[key]
    output = []
    if not context.media_types or "audio" in context.media_types:
        if metadata_only:
            metadata.update(metadata_only=True, formats_required=True)
            output.append(Media(url=webpage, title=title, id=identifier, kind="audio", source_url=source,
                                headers=context.scoped_headers(webpage), metadata=metadata))
        else:
            if not client:
                raise OmniError("SoundCloud playback client metadata is unavailable")
            formats, warnings = _formats(track, client, context)
            if not formats:
                raise OmniError("SoundCloud has no full publicly playable native formats; playback may be restricted or require unsupported protocols")
            if warnings:
                metadata["format_warnings"] = warnings
            first = formats[0]
            output.append(Media(url=first.url, title=title, id=identifier, kind="audio", ext=first.ext,
                                source_url=source, headers=first.headers, metadata=metadata, formats=formats))
    if artwork and context.media_types and "image" in context.media_types:
        _, extension = classify(artwork)
        artwork_metadata = {key: value for key, value in metadata.items() if key not in {"formats_required", "metadata_only"}}
        output.append(Media(url=artwork, title=title + " artwork", id=identifier + "-artwork", kind="image",
                            ext=extension or "jpg", source_url=source, headers=context.scoped_headers(artwork),
                            metadata={**artwork_metadata, "archive_id": "soundcloud:" + identifier + ":artwork"}))
    return output


def _resource(hydration: list[dict], canonical: str, is_set: bool) -> dict | None:
    values = [entry.get("data") for entry in hydration
              if entry.get("hydratable") == ("playlist" if is_set else "sound") and isinstance(entry.get("data"), dict)]
    for value in values:
        try:
            if _page_url(value.get("permalink_url", ""), short=False) == canonical:
                return value
        except (OmniError, TypeError, ValueError):
            continue
    if len(values) == 1 and not values[0].get("permalink_url"):
        return values[0]
    return None


def extract(url: str, context: ExtractorContext) -> list[Media]:
    requested = _page_url(url)
    if type(context.max_items) is not int or context.max_items < 1:
        raise OmniError("SoundCloud max_items must be positive")
    if context.media_types and not context.media_types.intersection({"audio", "image"}):
        return []
    raw, final, _ = context.get_document(requested)
    canonical = _page_url(final, short=False)
    page = raw.decode("utf-8-sig", "replace")
    hydration = _hydration(page)
    is_set = "/sets/" in urlsplit(canonical).path
    resource = _resource(hydration, canonical, is_set)
    metadata_only = getattr(context, "metadata_only", False)
    needs_audio = not metadata_only and (not context.media_types or "audio" in context.media_types)
    client = _client_id(hydration, page, canonical, context) if needs_audio or resource is None else None
    if resource is None:
        resource = _api("resolve", client, context, url=canonical)
    if not isinstance(resource, dict) or resource.get("kind") not in ("playlist" if is_set else "track", None):
        raise OmniError("SoundCloud returned unexpected public resource metadata")
    if not is_set:
        return _track(resource, client, url, context)[:context.max_items]

    _public(resource, "playlist", playback=not metadata_only)
    playlist_id = _identifier(resource.get("id"))
    if not playlist_id:
        raise OmniError("SoundCloud returned an invalid playlist identifier")
    tracks = resource.get("tracks")
    if not isinstance(tracks, list):
        raise OmniError("SoundCloud returned invalid playlist tracks")
    count = _playlist_count(resource, len(tracks))
    if len(tracks) < min(count, context.max_items):
        client = client or _client_id(hydration, page, canonical, context)
        refreshed = _api("playlists/" + playlist_id, client, context)
        if not isinstance(refreshed, dict) or _identifier(refreshed.get("id")) != playlist_id:
            raise OmniError("SoundCloud returned a mismatched playlist")
        _public(refreshed, "playlist", playback=not metadata_only)
        if not isinstance(refreshed.get("tracks"), list):
            raise OmniError("SoundCloud returned invalid playlist tracks")
        resource, tracks = refreshed, refreshed["tracks"]
        count = max(count, _playlist_count(resource, len(tracks)))
    selected = tracks[:context.max_items]
    known = {}
    missing = []
    for entry in selected:
        identifier = _identifier(entry.get("id")) if isinstance(entry, dict) else _identifier(entry)
        if not identifier:
            continue
        if isinstance(entry, dict) and (isinstance(entry.get("media"), dict)
                or metadata_only and isinstance(entry.get("title"), str)
                or not needs_audio and ("artwork_url" in entry or isinstance(entry.get("user"), dict))):
            known[identifier] = entry
        elif identifier not in missing:
            missing.append(identifier)
    lookup_errors = []
    if missing:
        client = client or _client_id(hydration, page, canonical, context)
    for index in range(0, len(missing), 50):
        ids = missing[index:index + 50]
        try:
            records = _api("tracks", client, context, ids=",".join(ids))
            if not isinstance(records, list):
                raise OmniError("SoundCloud returned an invalid track batch")
            for record in records:
                identifier = _identifier(record.get("id")) if isinstance(record, dict) else None
                if not identifier or identifier not in ids or identifier in known:
                    raise OmniError("SoundCloud returned an unexpected track batch record")
                known[identifier] = record
        except OmniError as exc:
            lookup_errors.append(str(exc))
    output, unavailable, seen = [], [], set()
    processed, truncated_assets = 0, False
    for index, entry in enumerate(selected, 1):
        processed = index
        identifier = _identifier(entry.get("id")) if isinstance(entry, dict) else _identifier(entry)
        if identifier and identifier in seen:
            continue
        if identifier:
            seen.add(identifier)
        if not identifier or identifier not in known:
            unavailable.append(identifier or f"index-{index}")
            continue
        try:
            assets = _track(known[identifier], client, url, context)
        except OmniError as exc:
            unavailable.append(identifier)
            lookup_errors.append(str(exc))
            continue
        if len(output) + len(assets) > context.max_items:
            truncated_assets = True
        for item in assets:
            playlist_title = resource.get("title") if isinstance(resource.get("title"), str) else playlist_id
            item.metadata.update(playlist_id=playlist_id, playlist_title=playlist_title,
                                 playlist_index=index, playlist_count=count)
            output.append(item)
            if len(output) >= context.max_items:
                break
        if len(output) >= context.max_items:
            break
    limited = count > len(selected) or processed < len(selected) or truncated_assets
    incomplete = len(tracks) < count or bool(unavailable) or bool(lookup_errors)
    for item in output:
        item.metadata.update(collection_complete=not limited and not incomplete, limited=bool(limited),
                             unavailable_track_ids=unavailable, extraction_warnings=lookup_errors)
    if not output and count and needs_audio:
        raise OmniError("SoundCloud set has no available public native tracks")
    return output
