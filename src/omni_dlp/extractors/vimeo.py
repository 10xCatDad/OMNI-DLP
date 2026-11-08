"""Progressive files and manifest descriptors from public Vimeo player data."""
from __future__ import annotations

import json
import math
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit

from ..models import Format, Media, OmniError
from ..transfer import validate_url
from ..types import classify
from .common import ExtractorContext


def _identity(url: str) -> tuple[str, str] | None:
    try:
        validate_url(url)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        if host == "player.vimeo.com":
            match = re.fullmatch(r"/video/(\d+)/?", parsed.path)
        elif host in {"vimeo.com", "www.vimeo.com"}:
            match = re.fullmatch(r"/(\d+)(?:/([A-Za-z0-9]+))?/?", parsed.path)
            if not match:
                match = re.fullmatch(r"/(?:channels/[^/]+|groups/[^/]+/videos|(?:album|showcase)/\d+/video)/(\d+)/?", parsed.path)
        else:
            return None
        if not match:
            return None
        access_hash = parse_qs(parsed.query).get("h", [""])[0]
        if not access_hash and match.lastindex and match.lastindex > 1:
            access_hash = match.group(2) or ""
        if access_hash and not re.fullmatch(r"[A-Za-z0-9]{1,128}", access_hash):
            return None
        return match.group(1), access_hash
    except (ValueError, OmniError):
        return None


def supports(url: str) -> bool:
    return _identity(url) is not None


def _http(value, base="https://player.vimeo.com/") -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        value = urljoin(base, value)
        validate_url(value)
        return value
    except (OmniError, ValueError):
        return None


def _number(value, *, integer=False):
    try:
        number = float(value) if not isinstance(value, bool) else float("nan")
        return (int(number) if integer else number) if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def _embedded(page: str) -> dict:
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\b(?:playerConfig|config)\s*=\s*(?=\{)", page):
        try:
            value, _ = decoder.raw_decode(page[match.end():])
        except (ValueError, RecursionError):
            continue
        if isinstance(value, dict) and ("video" in value or "view" in value):
            return value
    raise OmniError("Vimeo public player configuration is unavailable; account, password or embed access may be required")


def _config(identifier: str, access_hash: str, context: ExtractorContext) -> dict:
    player = f"https://player.vimeo.com/video/{identifier}"
    query = "?" + urlencode({"h": access_hash}) if access_hash else ""
    try:
        value = context.get_json(player + "/config" + query)
    except OmniError:
        value = _embedded(context.get_text(player + query))
    if not isinstance(value, dict):
        raise OmniError("Vimeo returned invalid player configuration")
    if value.get("view") == 4 or value.get("view") == "4":
        raise OmniError("Vimeo video requires a password; native password submission is not implemented")
    if value.get("message") and not isinstance(value.get("video"), dict):
        raise OmniError("Vimeo reports that this video is unavailable or access-restricted")
    return value


def _manifest_url(value, protocol: str, context: ExtractorContext) -> str | None:
    candidate = _http(value)
    if not candidate:
        return None
    # Some Vimeo DASH descriptors return JSON that points at the actual MPD.
    parsed = urlsplit(candidate)
    if protocol == "dash" and parse_qs(parsed.query).get("json") == ["1"]:
        value = context.get_json(candidate)
        candidate = _http(value.get("url")) if isinstance(value, dict) else None
        if not candidate:
            return None
        parsed = urlsplit(candidate)
    if protocol == "dash" and parsed.path.endswith("/master.json"):
        candidate = urlunsplit(parsed._replace(path=parsed.path[:-len("master.json")] + "master.mpd"))
    return candidate


def extract(url: str, context: ExtractorContext) -> list[Media]:
    identity = _identity(url)
    if not identity:
        raise OmniError("Unsupported Vimeo URL; use a video or public player URL")
    if context.max_items < 1:
        raise OmniError("max_items must be positive")
    identifier, access_hash = identity
    config = _config(identifier, access_hash, context)
    video = config.get("video") if isinstance(config.get("video"), dict) else {}
    request = config.get("request") if isinstance(config.get("request"), dict) else {}
    actual_id = video.get("id")
    if actual_id is not None and str(actual_id) != identifier:
        raise OmniError("Vimeo player returned a different video identifier")
    files = video.get("files") if isinstance(video.get("files"), dict) else request.get("files")
    files = files if isinstance(files, dict) else {}
    title = video.get("title") if isinstance(video.get("title"), str) else f"Vimeo {identifier}"
    formats = []
    seen = set()
    progressive = files.get("progressive")
    if isinstance(progressive, list):
        for index, item in enumerate(progressive):
            if not isinstance(item, dict) or item.get("drm") or item.get("has_drm"):
                continue
            candidate = _http(item.get("url"))
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            _, extension = classify(candidate, item.get("mime") if isinstance(item.get("mime"), str) else "")
            codec = item.get("codec") if isinstance(item.get("codec"), str) else ""
            formats.append(Format(url=candidate, format_id="http-" + str(item.get("quality") or index),
                                  ext=extension or "mp4", vcodec=codec, width=_number(item.get("width"), integer=True),
                                  height=_number(item.get("height"), integer=True), fps=_number(item.get("fps")),
                                  tbr=_number(item.get("bitrate")), filesize=_number(item.get("size"), integer=True),
                                  headers=context.scoped_headers(candidate)))
    diagnostics = []
    for protocol in ("hls", "dash"):
        manifest = files.get(protocol)
        if not isinstance(manifest, dict) or manifest.get("drm"):
            continue
        cdns = manifest.get("cdns")
        if not isinstance(cdns, dict):
            continue
        default = manifest.get("default_cdn")
        for cdn, descriptor in sorted(cdns.items(), key=lambda pair: (pair[0] != default, str(pair[0]))):
            if not isinstance(descriptor, dict):
                continue
            for key in ("url", "avc_url"):
                try:
                    candidate = _manifest_url(descriptor.get(key), protocol, context)
                except OmniError:
                    diagnostics.append(f"Could not resolve {protocol} descriptor")
                    continue
                if not candidate or candidate in seen:
                    continue
                seen.add(candidate)
                formats.append(Format(url=candidate, format_id=f"{protocol}-{cdn}" + ("-avc" if key == "avc_url" else ""),
                                      ext="mp4", protocol=protocol, headers=context.scoped_headers(candidate),
                                      metadata={"manifest": True, "cdn": str(cdn), "default_cdn": cdn == default,
                                                "drm_status": "unverified"}))
    thumbnails = []
    seen_thumbnails = set()
    thumbs = video.get("thumbs")
    candidates = list(thumbs.items()) if isinstance(thumbs, dict) else []
    candidates += [("thumbnail", video.get("thumbnail")), ("thumbnail_url", video.get("thumbnail_url"))]
    for key, value in candidates:
        image = _http(value)
        if image and image not in seen_thumbnails:
            seen_thumbnails.add(image)
            thumbnails.append({"id": str(key), "url": image, "width": _number(key, integer=True)})
    subtitles = {}
    tracks = request.get("text_tracks")
    if isinstance(tracks, list):
        for item in tracks:
            if not isinstance(item, dict):
                continue
            subtitle = _http(item.get("url"))
            language = item.get("lang")
            if subtitle and isinstance(language, str):
                subtitles.setdefault(language, []).append({"url": subtitle, "ext": "vtt", "name": item.get("label") if isinstance(item.get("label"), str) else language})
    chapters = []
    embed = config.get("embed") if isinstance(config.get("embed"), dict) else {}
    chapter_data = embed.get("chapters")
    if isinstance(chapter_data, list):
        for chapter in chapter_data:
            if not isinstance(chapter, dict):
                continue
            start = _number(chapter.get("timecode"))
            if start is not None and isinstance(chapter.get("title"), str):
                chapters.append({"title": chapter["title"], "start_time": start})
    owner = video.get("owner") if isinstance(video.get("owner"), dict) else {}
    metadata = {"extractor": "vimeo", "archive_id": f"vimeo:{identifier}", "webpage_url": url,
                "duration": _number(video.get("duration")), "uploader": owner.get("name") if isinstance(owner.get("name"), str) else "",
                "uploader_url": _http(owner.get("url")), "thumbnails": thumbnails, "subtitles": subtitles,
                "chapters": sorted(chapters, key=lambda value: value["start_time"]), "diagnostics": diagnostics}
    output = []
    if not context.media_types or "video" in context.media_types:
        if not formats:
            raise OmniError("Vimeo has no accessible clear video formats; private, paid, or DRM playback is not implemented")
        first = formats[0]
        output.append(Media(url=first.url, title=title, id=identifier, kind="video", ext=first.ext,
                            source_url=url, headers=first.headers, metadata=metadata, formats=formats))
    if context.media_types and "image" in context.media_types:
        for index, thumb in enumerate(thumbnails, 1):
            _, ext = classify(thumb["url"])
            output.append(Media(url=thumb["url"], title=title + " thumbnail", id=f"{identifier}-thumbnail-{index}",
                                kind="image", ext=ext or "jpg", source_url=url, headers=context.scoped_headers(thumb["url"]),
                                metadata={"extractor": "vimeo", "archive_id": f"vimeo:{identifier}:thumbnail:{index}"}))
    if context.media_types and "subtitle" in context.media_types:
        for language, tracks in subtitles.items():
            for index, track in enumerate(tracks, 1):
                output.append(Media(url=track["url"], title=f"{title} {language}", id=f"{identifier}-{language}-{index}",
                                    kind="subtitle", ext="vtt", source_url=url, headers=context.scoped_headers(track["url"]),
                                    metadata={"extractor": "vimeo", "archive_id": f"vimeo:{identifier}:subtitle:{language}:{index}", "language": language}))
    return output[:context.max_items]
