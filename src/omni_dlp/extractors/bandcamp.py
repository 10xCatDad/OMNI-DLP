"""Bandcamp's public track/album player data, parsed without JavaScript execution."""
from __future__ import annotations

import json
import math
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from ..models import Format, Media, OmniError
from ..transfer import validate_url
from ..types import classify
from .common import ExtractorContext


def supports(url: str) -> bool:
    try:
        validate_url(url)
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower()
        return (host == "bandcamp.com" or host.endswith(".bandcamp.com")) and bool(
            re.fullmatch(r"/(?:track|album)/[^/]+/?", parsed.path))
    except (OmniError, ValueError):
        return False


def _http(value, base: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        value = urljoin(base, value)
        validate_url(value)
        return value
    except (OmniError, ValueError):
        return None


def _number(value, *, integer: bool = False):
    try:
        number = float(value) if not isinstance(value, bool) else float("nan")
        return (int(number) if integer else number) if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class _Page(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tralbum: str | None = None
        self.embed: str | None = None
        self.image: str | None = None

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if self.tralbum is None and isinstance(values.get("data-tralbum"), str):
            self.tralbum = values["data-tralbum"]
        if self.embed is None and isinstance(values.get("data-embed"), str):
            self.embed = values["data-embed"]
        if tag == "meta" and values.get("property", "").lower() == "og:image":
            self.image = values.get("content")


def _object(raw: str | None, *, required: bool = True) -> dict:
    try:
        result = json.loads(raw or "{}")
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, RecursionError) as exc:
        if not required:
            return {}
        raise OmniError("Bandcamp returned invalid embedded player data") from exc


def _formats(track: dict, url: str, context: ExtractorContext) -> list[Format]:
    files = track.get("file")
    if not isinstance(files, dict):
        return []
    formats = []
    seen = set()
    for key, value in files.items():
        candidate = _http(value, url)
        if not candidate or candidate in seen or not isinstance(key, str):
            continue
        seen.add(candidate)
        codec, _, bitrate = key.partition("-")
        _, detected = classify(candidate)
        extension = codec if codec in {"mp3", "aac", "flac", "wav", "ogg", "opus", "m4a", "aiff", "alac"} else detected
        formats.append(Format(url=candidate, format_id=key, ext=extension,
                              vcodec="none", acodec=codec, abr=_number(bitrate),
                              headers=context.scoped_headers(candidate)))
    return formats


def extract(url: str, context: ExtractorContext) -> list[Media]:
    if not supports(url):
        raise OmniError("Unsupported Bandcamp URL; use an artist track or album page")
    if context.max_items < 1:
        raise OmniError("max_items must be positive")
    page_text = context.get_text(url)
    page = _Page()
    try:
        page.feed(page_text)
    except (ValueError, RecursionError) as exc:
        raise OmniError("Bandcamp returned invalid HTML player data") from exc
    if page.tralbum is None:
        if "client challenge" in page_text.lower() or "javascript is disabled" in page_text.lower():
            raise OmniError("Bandcamp requires a browser challenge; native extraction cannot complete this page")
        raise OmniError("Bandcamp player data is unavailable; this page may require account access")
    data = _object(page.tralbum)
    current = data.get("current") if isinstance(data.get("current"), dict) else {}
    embed = _object(page.embed, required=False)
    album = current.get("title") if isinstance(current.get("title"), str) else ""
    artist = next((v for v in (embed.get("artist"), current.get("artist"), data.get("artist")) if isinstance(v, str)), "")
    album_id = str(current.get("id") or data.get("id") or urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1])
    image = _http(page.image, url)
    tracks = data.get("trackinfo")
    if not isinstance(tracks, list):
        tracks = []
    is_album = urlsplit(url).path.startswith("/album/")
    if not is_album and len(tracks) > 1:
        matched = [track for track in tracks if isinstance(track, dict) and (
            urlsplit(_http(track.get("title_link"), url) or "").path.rstrip("/") == urlsplit(url).path.rstrip("/")
            or str(track.get("track_id") or track.get("id")) == str(current.get("id")))]
        if not matched:
            raise OmniError("Bandcamp track page did not identify a unique matching track")
        tracks = matched[:1]
    output = []
    unavailable = []
    seen_ids = set()
    for index, track in enumerate(tracks, 1):
        if not isinstance(track, dict):
            unavailable.append(str(index))
            continue
        identifier = str(track.get("track_id") or track.get("id") or f"{album_id}-{index}")
        if identifier in seen_ids:
            continue
        seen_ids.add(identifier)
        formats = _formats(track, url, context)
        if not formats:
            unavailable.append(identifier)
            # A valid public track record can still expose artwork and text.
            # Do not turn malformed player entries into invented tracks.
            has_metadata = isinstance(track.get("title"), str) and bool(track["title"].strip())
            if not context.metadata_only or not has_metadata:
                continue
        if context.media_types and "audio" not in context.media_types:
            continue
        title = track.get("title") if isinstance(track.get("title"), str) else album or identifier
        metadata = {"extractor": "bandcamp", "archive_id": f"bandcamp:{identifier}",
                    "webpage_url": _http(track.get("title_link"), url) or url,
                    "artist": artist, "duration": _number(track.get("duration")),
                    "track_number": _number(track.get("track_num"), integer=True) or index,
                    "thumbnails": [{"url": image}] if image else []}
        description = next((value for value in (track.get("about"), track.get("description"), current.get("about"))
                            if isinstance(value, str) and value), None)
        if description is not None:
            metadata["description"] = description
        if is_album:
            metadata.update(album=album, playlist_id=album_id, playlist_title=album,
                            playlist_index=index, playlist_count=len(tracks))
        first = formats[0] if formats else None
        if first is None:
            metadata.update(metadata_only=True, formats_required=True)
        output.append(Media(url=first.url if first else metadata["webpage_url"], title=title,
                            id=identifier, kind="audio", ext=first.ext if first else "bin", source_url=url,
                            headers=first.headers if first else context.scoped_headers(metadata["webpage_url"]),
                            metadata=metadata, formats=formats))
        if len(output) >= context.max_items:
            break
    if image and context.media_types and "image" in context.media_types and len(output) < context.max_items:
        kind, ext = classify(image)
        output.append(Media(url=image, title=(album or album_id) + " cover", id=album_id + "-cover",
                            kind="image", ext=ext or "jpg", source_url=url, headers=context.scoped_headers(image),
                            metadata={"extractor": "bandcamp", "archive_id": f"bandcamp:{album_id}:cover", "artist": artist}))
    for media in output:
        if is_album:
            media.metadata["collection_complete"] = not unavailable and len(tracks) <= context.max_items
            media.metadata["unavailable_track_ids"] = unavailable
            media.metadata["limited"] = len(tracks) > context.max_items
    if not output and (not context.media_types or "audio" in context.media_types):
        raise OmniError("Bandcamp has no publicly playable audio formats; purchase/account downloads are not implemented")
    return output
