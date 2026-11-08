"""Independent public Reddit post, gallery, and hosted-media extraction."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import html
import json
import math
from pathlib import PurePosixPath
import re
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from ..models import Format, Media, OmniError
from ..transfer import validate_url
from ..types import classify
from .common import ExtractorContext

_POST = re.compile(r"/(?:r/[^/]+/|(?:user|u)/[^/]+/)?comments/([a-z0-9]{3,16})(?:[/.]|$)", re.I)
_REDDIT = re.compile(r"(?:[a-z0-9-]+\.)?reddit(?:media)?\.com\Z", re.I)
_ID = re.compile(r"[a-z0-9]{3,32}\Z", re.I)
_MAX_NODES = 20
_MAX_DEPTH = 4
_MAX_ASSETS = 5000


def _location(url: str):
    try:
        validate_url(url)
        parsed = urlsplit(url)
        if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
            return None
        host = (parsed.hostname or "").lower()
        path = unquote(parsed.path)
        if any(ord(c) < 32 for c in path) or any(p in {".", ".."} for p in path.split("/")):
            return None
        if host == "i.redd.it" and path.strip("/") and path.count("/") == 1:
            return "image", PurePosixPath(path).stem
        if host == "v.redd.it":
            parts = path.strip("/").split("/")
            if parts and _ID.fullmatch(parts[0]):
                return ("video" if len(parts) == 1 else "video-file"), parts[0]
        if host == "redd.it" and _ID.fullmatch(path.strip("/")):
            return "post", path.strip("/").lower()
        if not _REDDIT.fullmatch(host):
            return None
        match = _POST.match(path)
        if not match:
            match = re.match(r"/gallery/([a-z0-9]{3,16})(?:[/.]|$)", path, re.I)
        return ("post", match[1].lower()) if match else None
    except (TypeError, ValueError, OmniError):
        return None


def supports(url: str) -> bool:
    return _location(url) is not None


def _url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        value = html.unescape(value)
        if value.startswith("//"):
            value = "https:" + value
        validate_url(value)
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
    except (OmniError, ValueError):
        return None


def _number(value, *, integer=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
        if not math.isfinite(number) or number < 0 or (integer and not number.is_integer()):
            return None
        return int(number) if integer else number
    except (ValueError, TypeError, OverflowError):
        return None


def _text(value, default=""):
    return value if isinstance(value, str) else default


def _safe_id(value: str) -> str:
    return value if re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) else hashlib.sha256(value.encode()).hexdigest()[:16]


def _asset_key(url: str) -> str:
    parsed = urlsplit(url)
    return hashlib.sha256((str(parsed.hostname) + parsed.path).encode()).hexdigest()[:16]


def _thumbnails(post: dict) -> list[dict]:
    found = []
    seen = set()
    preview = post.get("preview")
    images = preview.get("images", []) if isinstance(preview, dict) else []
    for image in images[:100] if isinstance(images, list) else []:
        if not isinstance(image, dict):
            continue
        candidates = [image.get("source")]
        if isinstance(image.get("resolutions"), list):
            candidates.extend(image["resolutions"][:100])
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            url = _url(candidate.get("url"))
            if url and url not in seen:
                seen.add(url)
                found.append({"url": url, "width": _number(candidate.get("width"), integer=True),
                              "height": _number(candidate.get("height"), integer=True)})
    thumbnail = _url(post.get("thumbnail"))
    if thumbnail and thumbnail not in seen:
        found.append({"url": thumbnail})
    return found


def _video(data: dict, context: ExtractorContext, metadata: dict, title: str, fallback_id: str) -> Media | None:
    urls = {
        "hls": _url(data.get("hls_url") or data.get("hlsUrl")),
        "dash": _url(data.get("dash_url") or data.get("dashUrl")),
        "fallback": _url(data.get("fallback_url") or data.get("fallbackUrl") or data.get("mp4")),
    }
    valid = [value for value in urls.values() if value]
    if not valid:
        return None
    video_id = next((urlsplit(value).path.strip("/").split("/", 1)[0] for value in valid
                     if urlsplit(value).hostname == "v.redd.it"), fallback_id)
    video_id = _safe_id(video_id)
    formats = []
    for protocol, url in urls.items():
        if not url:
            continue
        formats.append(Format(
            url=url, format_id=protocol, ext="mp4", protocol="http" if protocol == "fallback" else protocol,
            vcodec="h264" if protocol == "fallback" else "",
            acodec="none" if protocol == "fallback" or data.get("has_audio") is False or data.get("is_gif") is True else "",
            width=_number(data.get("width"), integer=True), height=_number(data.get("height"), integer=True),
            tbr=_number(data.get("bitrate_kbps")), headers=context.scoped_headers(url),
        ))
    details = {**metadata, "extractor": "reddit", "archive_id": "reddit:video:" + video_id,
               "duration": _number(data.get("duration")), "is_gif": data.get("is_gif") is True}
    caption = _url(data.get("caption_url") or data.get("captionUrl"))
    if caption:
        language = data.get("caption_language") or data.get("captionLanguage")
        language = language if isinstance(language, str) and re.fullmatch(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language) else "und"
        details["subtitles"] = {language: [{"url": caption, "ext": "vtt"}]}
    return Media(formats[0].url, title=title, id=video_id, kind="video", ext="mp4",
                 source_url=context.source_url, headers=formats[0].headers, metadata=details, formats=formats)


def _direct_asset(url: str, context: ExtractorContext, metadata: dict, title: str,
                  identifier: str, mime: str = "") -> Media:
    kind, ext = classify(url, mime)
    formats = []
    if kind in {"audio", "video"}:
        formats = [Format(url, format_id="original", ext=ext,
                          protocol="hls" if ext == "m3u8" else "dash" if ext == "mpd" else "http",
                          vcodec="none" if kind == "audio" else "", acodec="",
                          headers=context.scoped_headers(url))]
    return Media(url, title=title, id=_safe_id(identifier), kind=kind, ext=ext, mime_type=mime,
                 source_url=context.source_url, headers=context.scoped_headers(url),
                 metadata={**metadata, "extractor": "reddit", "archive_id": "reddit:asset:" + identifier},
                 formats=formats)


def _hosted_video(url: str, identifier: str, context: ExtractorContext) -> Media:
    available = {}
    headers = {}
    failures = []
    for kind, filename in (("hls", "HLSPlaylist.m3u8"), ("dash", "DASHPlaylist.mpd")):
        target = f"https://v.redd.it/{quote(identifier, safe='')}/{filename}"
        try:
            body, final, outgoing = context.get_document(target)
            if kind == "hls" and not body.lstrip().startswith(b"#EXTM3U"):
                raise OmniError("Reddit video endpoint returned an invalid HLS manifest")
            if kind == "dash" and not re.search(rb"<(?:[A-Za-z_][\w.-]*:)?MPD\b", body[:65536]):
                raise OmniError("Reddit video endpoint returned an invalid DASH manifest")
            final = _url(final)
            if not final:
                raise OmniError("Reddit video endpoint returned an invalid URL")
            available[kind + "_url"] = final
            headers[kind] = outgoing
        except OmniError as exc:
            failures.append(str(exc))
    if not available:
        raise OmniError("Reddit hosted video has no accessible HLS or DASH manifest")
    metadata = {"webpage_url": url, "metadata_unavailable": True, "format_warnings": failures}
    result = _video(available, context, metadata, identifier, identifier)
    if result is None:
        raise OmniError("Reddit video has no usable formats")
    result.formats = [replace(fmt, headers=headers[fmt.format_id]) for fmt in result.formats]
    result.headers = result.formats[0].headers
    return result


def _post(payload, expected: str) -> dict:
    if isinstance(payload, dict) and (payload.get("error") or payload.get("reason")):
        raise OmniError("Reddit reports that this post is unavailable, restricted, or requires authentication")
    listings = payload if isinstance(payload, list) else [payload]
    for listing in listings[:3]:
        data = listing.get("data") if isinstance(listing, dict) else None
        children = data.get("children") if isinstance(data, dict) else None
        for child in children[:100] if isinstance(children, list) else []:
            post = child.get("data") if isinstance(child, dict) and child.get("kind") == "t3" else None
            if isinstance(post, dict) and post.get("id") == expected:
                return post
    raise OmniError("Reddit response did not contain the requested post")


def extract(url: str, context: ExtractorContext) -> list[Media]:
    location = _location(url)
    if location is None:
        raise OmniError("Unsupported Reddit URL")
    if type(context.max_items) is not int or context.max_items < 1:
        raise OmniError("Reddit max_items must be positive")
    mode, identifier = location
    if mode == "image":
        result = _direct_asset(url, context, {}, identifier, "image:" + _asset_key(url))
        if result.kind == "other":
            result.kind = "image"
        return [result] if not context.media_types or result.kind in context.media_types else []
    if mode == "video-file":
        kind, ext = classify(url)
        result = _direct_asset(url, context, {}, identifier, "video:" + identifier + ":" + _asset_key(url))
        if "audio" in PurePosixPath(urlsplit(url).path).name.lower() and ext in {"mp4", "m4a", "aac"}:
            result.kind = "audio"
            result.formats = [replace(f, vcodec="none", acodec="") for f in result.formats]
        elif kind == "other":
            raise OmniError("Unsupported Reddit video resource URL")
        return [result] if not context.media_types or result.kind in context.media_types else []
    if mode == "video":
        result = _hosted_video(url, identifier, context)
        return [result] if not context.media_types or "video" in context.media_types else []

    requested_host = urlsplit(url).hostname or "www.reddit.com"
    api_host = requested_host if _REDDIT.fullmatch(requested_host) else "www.reddit.com"
    endpoint = f"https://{api_host}/comments/{identifier}.json?raw_json=1&limit=0"
    body, final, _headers = context.get_document(endpoint)
    if re.search(r"/(?:login|register)(?:[/?]|$)", urlsplit(final).path):
        raise OmniError("Reddit redirected the public post request to login; public metadata is unavailable")
    try:
        post = _post(json.loads(body), identifier)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise OmniError("Reddit did not return public post JSON; authentication or network access may be required") from exc
    base = {"extractor": "reddit", "post_id": identifier, "webpage_url": f"https://www.reddit.com/comments/{identifier}",
            "description": _text(post.get("selftext")), "uploader": _text(post.get("author")),
            "channel_id": _text(post.get("subreddit")), "timestamp": _number(post.get("created_utc")),
            "age_limit": 18 if post.get("over_18") is True else 0, "thumbnails": _thumbnails(post),
            "comment_count": _number(post.get("num_comments"), integer=True)}
    title = _text(post.get("title"), identifier)
    assets: list[Media] = []
    seen_assets = set()
    seen_posts = set()
    slots = 0
    omitted = 0
    incomplete = False
    hosted_probes = 0

    def add(group: list[Media], *, position: int | None = None):
        nonlocal slots
        group = [asset for asset in group if asset.metadata["archive_id"] not in seen_assets]
        if not group:
            return
        if position is None:
            slots += 1
            position = slots
        for asset in group:
            key = asset.metadata["archive_id"]
            if key in seen_assets:
                continue
            seen_assets.add(key)
            asset.metadata.update(playlist_index=position)
            assets.append(asset)

    def entry(data: dict, media_id: str, details: dict) -> list[Media]:
        nonlocal omitted
        if data.get("status") not in (None, "valid"):
            omitted += 1
            return []
        kind = data.get("e")
        if kind == "RedditVideo":
            video = _video(data, context, details, title, media_id)
            if video:
                return [video]
        source = data.get("s")
        source = source if isinstance(source, dict) else {}
        details = {**details, "width": _number(source.get("x"), integer=True),
                   "height": _number(source.get("y"), integer=True)}
        mime = _text(data.get("m"))
        result = []
        original = _url(source.get("gif") if kind == "AnimatedImage" else source.get("u"))
        if original:
            result.append(_direct_asset(original, context, details, title, identifier + ":" + media_id + ":original", mime))
        alternate = _url(source.get("mp4"))
        if alternate and alternate != original:
            video = _direct_asset(alternate, context, details, title, identifier + ":" + media_id + ":mp4", "video/mp4")
            video.formats = [replace(f, acodec="none") for f in video.formats]
            result.append(video)
        if not result:
            omitted += 1
        return result

    def visit(node: dict, depth: int):
        nonlocal slots, omitted, incomplete, hosted_probes
        key = node.get("id")
        if not isinstance(key, str) or not _ID.fullmatch(key):
            omitted += 1
            return
        if key in seen_posts:
            return
        if depth > _MAX_DEPTH or len(seen_posts) >= _MAX_NODES:
            incomplete = True
            return
        seen_posts.add(key)
        details = {**base, "source_post_id": key, "source_uploader": _text(node.get("author"))}
        start_count = len(assets)
        for field in ("secure_media", "media"):
            source = node.get(field)
            source = source.get("reddit_video") if isinstance(source, dict) else None
            if isinstance(source, dict):
                video = _video(source, context, details, title, key)
                if video:
                    add([video])
        values = node.get("media_metadata")
        values = values if isinstance(values, dict) else {}
        gallery = node.get("gallery_data")
        entries = gallery.get("items") if isinstance(gallery, dict) else None
        if isinstance(entries, list):
            base_position = slots
            slots += len(entries)
            incomplete |= len(entries) > _MAX_ASSETS
            for index, member in enumerate(entries[:_MAX_ASSETS]):
                media_id = member.get("media_id") if isinstance(member, dict) else None
                value = values.get(media_id) if isinstance(media_id, str) else None
                if not isinstance(value, dict):
                    omitted += 1
                    continue
                detail = {**details, "caption": _text(member.get("caption")), "gallery_media_id": media_id}
                add(entry(value, media_id, detail), position=base_position + index + 1)
        elif values:
            base_position = slots
            slots += len(values)
            incomplete |= len(values) > _MAX_ASSETS
            for index, (media_id, value) in enumerate(list(values.items())[:_MAX_ASSETS]):
                if not isinstance(value, dict) or not isinstance(media_id, str):
                    omitted += 1
                    continue
                add(entry(value, media_id, details), position=base_position + index + 1)
        parents = node.get("crosspost_parent_list")
        if isinstance(parents, list):
            incomplete |= len(parents) > _MAX_NODES
            for parent in parents[:_MAX_NODES]:
                if isinstance(parent, dict):
                    visit(parent, depth + 1)
                else:
                    omitted += 1
        if len(assets) == start_count:
            target = _url(node.get("url_overridden_by_dest") or node.get("url"))
            if target:
                category, _ = classify(target)
                if category != "other" and not supports(target):
                    add([_direct_asset(target, context, details, title, identifier + ":" + _asset_key(target))])
                elif urlsplit(target).hostname == "i.redd.it":
                    add([_direct_asset(target, context, details, title, identifier + ":" + _asset_key(target))])
                elif (location := _location(target)) and location[0] == "video":
                    if hosted_probes >= 4:
                        incomplete = True
                    else:
                        hosted_probes += 1
                        try:
                            video = _hosted_video(target, location[1], context)
                            video.title = title
                            video.metadata = {**details, **video.metadata}
                            add([video])
                        except OmniError:
                            omitted += 1

    visit(post, 0)
    if not assets:
        if getattr(context, "metadata_only", False):
            return [Media(url, title=title, id=identifier, kind="other", source_url=context.source_url,
                          headers=context.scoped_headers(url), metadata={
                              **base, "archive_id": "reddit:post:" + identifier,
                              "metadata_only": True, "formats_required": True,
                              "collection_complete": not (incomplete or omitted), "collection_omitted": omitted})]
        raise OmniError("Reddit post has no available native media; external-page delegation is unsupported and hosted resources may be unavailable")
    filtered = [asset for asset in assets if not context.media_types or asset.kind in context.media_types]
    incomplete |= len(filtered) > context.max_items or omitted > 0
    result = filtered[:context.max_items]
    for asset in result:
        asset.metadata.update(playlist_id="reddit:" + identifier, playlist_title=title, playlist_count=slots,
                              collection_complete=not incomplete, collection_truncated=incomplete,
                              collection_omitted=omitted, extracted_assets=len(filtered))
    return result
