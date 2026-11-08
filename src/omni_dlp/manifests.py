"""Bounded, independent HLS format discovery following RFC 8216 section 4.3.4.

Only HTTP(S) resources with clear or ordinary AES-128 media are admitted. Master playlists become
selectable variants plus external audio renditions; a media playlist yields one
opaque HLS descriptor. Segment fetching remains the adaptive downloader's job.
I-frame/trick-play and subtitle renditions are not exposed as playable formats.
Specification: https://www.rfc-editor.org/rfc/rfc8216.html#section-4.3.4
"""
from __future__ import annotations

import math
import re
from urllib.parse import urldefrag, urljoin

from .models import Format, OmniError
from .transfer import _origin, request_headers, validate_url

_MAX_DOCUMENT = 8 * 1024 * 1024
_MAX_LINE = 65536
_MAX_FORMATS = 10000
_SAFE_HEADERS = {"user-agent", "accept", "accept-language", "accept-encoding"}
_VIDEO_CODECS = {"avc1", "avc3", "hvc1", "hev1", "dvh1", "dvhe", "vp09", "vp9", "av01", "mp4v", "h263"}
_AUDIO_CODECS = {"mp4a", "ac-3", "ec-3", "opus", "vorbis", "flac", "alac", "mp3", "aac"}


def _attributes(value: str) -> dict[str, str]:
    """Parse HLS attributes without splitting commas inside quoted strings."""
    result = {}
    index = 0
    while index < len(value):
        name = re.match(r"\s*([A-Z0-9-]+)\s*=\s*", value[index:])
        if not name:
            raise OmniError("Invalid HLS attribute list")
        key = name[1]
        if key in result:
            raise OmniError("Duplicate HLS attribute")
        index += name.end()
        if index >= len(value):
            raise OmniError("Missing HLS attribute value")
        if value[index] == '"':
            end = value.find('"', index + 1)
            if end < 0:
                raise OmniError("Unterminated HLS attribute string")
            item = value[index + 1:end]
            index = end + 1
            while index < len(value) and value[index].isspace():
                index += 1
            if index < len(value) and value[index] != ',':
                raise OmniError("Invalid HLS quoted attribute")
        else:
            end = value.find(',', index)
            if end < 0:
                end = len(value)
            item = value[index:end].strip()
            if not item or '"' in item:
                raise OmniError("Invalid HLS attribute value")
            index = end
        result[key] = item
        if index < len(value):
            index += 1
            if index == len(value):
                raise OmniError("Trailing comma in HLS attribute list")
    return result


def _number(value: str | None, name: str, *, integer: bool = False) -> float | int | None:
    if value is None:
        return None
    try:
        result = int(value) if integer else float(value)
        if not math.isfinite(result) or result <= 0:
            raise ValueError
    except (ValueError, OverflowError) as exc:
        raise OmniError(f"Invalid HLS {name}") from exc
    return result


def _uri(value: str, base: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise OmniError("Invalid HLS URI")
    # Variable substitution requires an explicit EXT-X-DEFINE scope. Never
    # request an unresolved placeholder and pretend it is a supported URL.
    if "{$" in value:
        raise OmniError("HLS variable substitution is not implemented")
    result = urldefrag(urljoin(base, value))[0]
    validate_url(result)
    return result


def _headers(headers: dict[str, str], origin: str, target: str) -> dict[str, str]:
    if _origin(origin) == _origin(target):
        return dict(headers)
    return {key: value for key, value in headers.items() if key.lower() in _SAFE_HEADERS}


def _codecs(value: str) -> tuple[str, str]:
    if not value:
        return "", ""
    names = [part.strip() for part in value.split(',') if part.strip()]
    video = next((name for name in names if name.split('.')[0].lower() in _VIDEO_CODECS), "")
    audio = next((name for name in names if name.split('.')[0].lower() in _AUDIO_CODECS), "")
    # Only mark an absent stream when all advertised codecs are understood.
    known = all(name.split('.')[0].lower() in _VIDEO_CODECS | _AUDIO_CODECS for name in names)
    return video or ("none" if known else ""), audio or ("none" if known else "")


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")[:80] or "unknown"


def parse_hls(text: str, url: str, *, headers: dict | None = None) -> list[Format]:
    """Extract HLS variants without fetching any subordinate resources.

    Malformed playlists and unsupported encryption raise ``OmniError``. A media
    playlist returns one descriptor whose codec/quality fields remain unknown.
    Master playlists return declared variants in source order, then audio
    renditions. External audio metadata identifies the compatible rendition
    group; matching language/group is the selection layer's responsibility.
    Header credentials are preserved only for URLs on the manifest's origin.
    """
    validate_url(url)
    if not isinstance(text, str) or len(text) > _MAX_DOCUMENT or len(text.encode("utf-8")) > _MAX_DOCUMENT:
        raise OmniError("HLS manifest exceeds the document limit")
    text = text.removeprefix("\ufeff")
    if any(ord(char) < 32 and char not in "\r\n\t" or ord(char) == 127 for char in text):
        raise OmniError("HLS manifest contains control characters")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "#EXTM3U":
        raise OmniError("Invalid HLS manifest header")
    if any(len(line) > _MAX_LINE for line in lines):
        raise OmniError("HLS manifest line exceeds the parser limit")
    base = urldefrag(url)[0]
    request = request_headers(headers)
    variants = []
    renditions = []
    pending = None
    media_playlist = False
    media_uri = False
    encrypted = False
    for raw in lines[1:]:
        line = raw.strip()
        if not line:
            continue
        if line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            attributes = _attributes(line.partition(':')[2])
            method = attributes.get("METHOD")
            if method == "NONE":
                if len(attributes) != 1 or line.startswith("#EXT-X-SESSION-KEY:"):
                    raise OmniError("Invalid HLS clear-key declaration")
            elif method == "AES-128":
                if attributes.get("KEYFORMAT", "identity") != "identity":
                    raise OmniError("Unsupported HLS key format; only ordinary identity-key AES-128 is supported")
                versions = attributes.get("KEYFORMATVERSIONS", "1")
                if not re.fullmatch(r"[1-9]\d*(?:/[1-9]\d*)*", versions) or "1" not in versions.split('/'):
                    raise OmniError("Unsupported HLS key format version")
                if "URI" not in attributes:
                    raise OmniError("HLS AES-128 key declaration requires a key URI")
                _uri(attributes["URI"], base)
                if "IV" in attributes and not re.fullmatch(r"0[xX][0-9a-fA-F]{1,32}", attributes["IV"]):
                    raise OmniError("Invalid HLS AES-128 initialization vector")
                encrypted = True
            else:
                raise OmniError("Unsupported HLS encryption; only clear media and ordinary AES-128 are supported")
        elif line.startswith("#EXT-X-STREAM-INF:"):
            if pending is not None:
                raise OmniError("HLS variant is missing its playlist URI")
            pending = _attributes(line.partition(':')[2])
            if "BANDWIDTH" not in pending:
                raise OmniError("HLS variant is missing BANDWIDTH")
        elif line.startswith("#EXT-X-MEDIA:"):
            attributes = _attributes(line.partition(':')[2])
            if "URI" in attributes:
                attributes["URI"] = _uri(attributes["URI"], base)
            if attributes.get("TYPE") == "AUDIO":
                if not attributes.get("GROUP-ID") or not attributes.get("NAME"):
                    raise OmniError("HLS audio rendition requires GROUP-ID and NAME")
                for key in ("DEFAULT", "AUTOSELECT"):
                    if key in attributes and attributes[key] not in {"YES", "NO"}:
                        raise OmniError(f"Invalid HLS {key}")
                if attributes.get("DEFAULT") == "YES" and attributes.get("AUTOSELECT") == "NO":
                    raise OmniError("Default HLS audio must permit automatic selection")
                renditions.append(attributes)
        elif line.startswith(("#EXT-X-I-FRAME-STREAM-INF:", "#EXT-X-SESSION-DATA:",
                              "#EXT-X-PART:", "#EXT-X-PRELOAD-HINT:", "#EXT-X-RENDITION-REPORT:")):
            attributes = _attributes(line.partition(':')[2])
            if "URI" in attributes:
                _uri(attributes["URI"], base)
        elif line.startswith(("#EXTINF:", "#EXT-X-TARGETDURATION:", "#EXT-X-MEDIA-SEQUENCE:")):
            media_playlist = True
        elif line.startswith("#EXT-X-MAP:"):
            attributes = _attributes(line.partition(':')[2])
            if "URI" not in attributes:
                raise OmniError("HLS initialization map requires a URI")
            _uri(attributes["URI"], base)
            media_playlist = True
        elif not line.startswith('#'):
            target = _uri(line, base)
            if pending is not None:
                variants.append((pending, target))
                pending = None
            else:
                media_uri = True
        if len(variants) + len(renditions) > _MAX_FORMATS:
            raise OmniError("HLS manifest contains too many formats")
    if pending is not None:
        raise OmniError("HLS variant is missing its playlist URI")
    if (variants or renditions) and (media_playlist or media_uri):
        raise OmniError("HLS manifest mixes master and media playlist entries")
    if not variants and not renditions:
        if not media_playlist:
            raise OmniError("HLS manifest declares no playable streams")
        return [Format(base, format_id="hls", ext="m3u8", protocol="hls", headers=request,
                       metadata={"manifest_url": base, "is_live": "#EXT-X-ENDLIST" not in text,
                                 "encryption": "AES-128" if encrypted else "none"})]

    groups: dict[str, list[dict[str, str]]] = {}
    for entry in renditions:
        groups.setdefault(entry["GROUP-ID"], []).append(entry)
    results: list[Format] = []
    ids: dict[str, int] = {}
    seen = set()

    def append(item: Format, identity):
        if identity in seen:
            return
        seen.add(identity)
        name = item.format_id
        ids[name] = ids.get(name, 0) + 1
        if ids[name] > 1:
            item.format_id += "-" + str(ids[name])
        results.append(item)

    for attributes, target in variants:
        width = height = None
        if "RESOLUTION" in attributes:
            match = re.fullmatch(r"([1-9]\d{0,5})x([1-9]\d{0,5})", attributes["RESOLUTION"])
            if not match:
                raise OmniError("Invalid HLS resolution")
            width, height = map(int, match.groups())
        bandwidth = _number(attributes["BANDWIDTH"], "bandwidth", integer=True)
        average = _number(attributes.get("AVERAGE-BANDWIDTH"), "average bandwidth", integer=True)
        fps = _number(attributes.get("FRAME-RATE"), "frame rate")
        video, audio = _codecs(attributes.get("CODECS", ""))
        group = attributes.get("AUDIO", "")
        if group and group not in groups:
            raise OmniError("HLS variant references an undeclared audio group")
        if group and all("URI" in rendition for rendition in groups[group]):
            audio = "none"
        if height:
            name = f"hls-{height}p"
        elif video == "none":
            name = f"hls-audio-{round((average or bandwidth) / 1000)}k"
        else:
            name = f"hls-{round((average or bandwidth) / 1000)}k"
        metadata = {"manifest_url": base, "audio_group": group,
                    "subtitles_group": attributes.get("SUBTITLES", ""),
                    "video_group": attributes.get("VIDEO", ""),
                    "codecs": attributes.get("CODECS", ""),
                    "encryption": "AES-128" if encrypted else "none"}
        candidate = Format(target, name, ext="m3u8", protocol="hls", vcodec=video,
                           acodec=audio, width=width, height=height, fps=fps,
                           tbr=(average or bandwidth) / 1000,
                           headers=_headers(request, base, target), metadata=metadata)
        append(candidate, (target, group, width, height, fps, video, audio))
    for attributes in renditions:
        if "URI" not in attributes:
            continue  # In-band audio is already carried by the video variant.
        group = attributes["GROUP-ID"]
        audio_codecs = {_codecs(variant.get("CODECS", ""))[1] for variant, _ in variants
                        if variant.get("AUDIO") == group}
        audio_codecs.discard("")
        audio_codecs.discard("none")
        codec = next(iter(audio_codecs)) if len(audio_codecs) == 1 else ""
        language = attributes.get("LANGUAGE", "")
        target = attributes["URI"]
        name = "hls-audio-" + _slug(group) + "-" + _slug(language or attributes["NAME"])
        candidate = Format(target, name, ext="m3u8", protocol="hls", vcodec="none", acodec=codec,
                           language=language, headers=_headers(request, base, target), metadata={
                               "manifest_url": base, "audio_group": group, "name": attributes["NAME"],
                               "default": attributes.get("DEFAULT") == "YES",
                               "autoselect": attributes.get("AUTOSELECT") == "YES",
                               "channels": attributes.get("CHANNELS", ""),
                               "encryption": "AES-128" if encrypted else "none",
                               "language_preference": 1 if attributes.get("DEFAULT") == "YES" else 0,
                           })
        append(candidate, (target, group, language, attributes["NAME"]))
    return results
