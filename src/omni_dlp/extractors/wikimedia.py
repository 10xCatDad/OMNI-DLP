"""Independent extraction of Wikimedia file originals and timed-media renditions."""
from __future__ import annotations

import hashlib
import math
from pathlib import PurePosixPath
import re
from urllib.parse import parse_qs, quote, unquote, urlencode, urljoin, urlsplit

from ..models import Format, Media, OmniError
from ..transfer import validate_url
from ..types import classify
from .common import ExtractorContext

_PROJECT = re.compile(r"[a-z0-9-]+(?:\.m)?\.(?:wikipedia|wiktionary|wikibooks|wikinews|wikiquote|wikisource|wikiversity|wikivoyage)\.org\Z")
_FIXED_HOSTS = {"commons.wikimedia.org", "commons.m.wikimedia.org", "www.mediawiki.org", "mediawiki.org"}
_INFO_PROPS = "url|size|mime|mediatype|sha1|derivatives|timedtext"


def _source(url: str) -> tuple[str, str]:
    validate_url(url)
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host not in _FIXED_HOSTS and not _PROJECT.fullmatch(host):
        raise OmniError("Not a supported Wikimedia host")
    if parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise OmniError("Wikimedia URLs must use the standard HTTP(S) port")
    if parsed.path.startswith("/wiki/"):
        title = unquote(parsed.path[len("/wiki/"):])
    elif parsed.path in {"/w/index.php", "/w/api.php"}:
        query = parse_qs(parsed.query)
        title = query.get("title", query.get("titles", [""]))[0]
    else:
        raise OmniError("Use a Wikimedia File or Category page URL")
    title = title.replace("_", " ")
    if not re.match(r"^(?:File|Image|Category):[^|]+$", title, re.I) or any(ord(c) < 32 for c in title):
        raise OmniError("Use a Wikimedia File or Category page URL")
    namespace, name = title.split(":", 1)
    namespace = "Category" if namespace.lower() == "category" else "File"
    if not name.strip():
        raise OmniError("Wikimedia page title is empty")
    # Mobile and desktop APIs identify the same wiki and archive namespace.
    canonical = host.replace(".m.", ".")
    if canonical == "mediawiki.org":
        canonical = "www.mediawiki.org"
    return canonical, namespace + ":" + name


def supports(url: str) -> bool:
    try:
        _source(url)
        return True
    except (OmniError, ValueError, TypeError):
        return False


def _query(host: str, context: ExtractorContext, **params) -> dict:
    result = context.get_json("https://" + host + "/w/api.php?" + urlencode({
        "action": "query", "format": "json", "formatversion": 2, **params,
    }))
    if not isinstance(result, dict):
        raise OmniError("Wikimedia returned an invalid API response")
    if "error" in result or "errors" in result:
        # Server error strings can echo request credentials or arbitrary HTML.
        raise OmniError("Wikimedia API rejected the request; the file may be unavailable or restricted")
    return result


def _pages(result: dict) -> list[dict]:
    query = result.get("query")
    pages = query.get("pages") if isinstance(query, dict) else None
    if not isinstance(pages, list) or not all(isinstance(page, dict) for page in pages):
        raise OmniError("Wikimedia returned invalid file metadata")
    return pages


def _number(value, *, integer=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if integer else number


def _media_url(value, host: str, *, subtitle=False) -> str:
    if not isinstance(value, str):
        raise OmniError("Wikimedia file has no downloadable URL")
    url = urljoin("https://" + host + "/", value)
    validate_url(url)
    parsed = urlsplit(url)
    allowed = {"upload.wikimedia.org"}
    if subtitle:
        allowed.add(host)
        allowed.add("commons.wikimedia.org")
    if parsed.hostname not in allowed or parsed.port not in {None, 80 if parsed.scheme == "http" else 443}:
        raise OmniError("Wikimedia returned a media URL outside its supported storage hosts")
    return url


def _mime(value) -> str:
    return value if isinstance(value, str) else ""


def _format(info: dict, url: str, format_id: str, kind: str, context: ExtractorContext,
            duration: float | None = None) -> Format:
    mime = _mime(info.get("type", info.get("mime")))
    _, ext = classify(url, mime)
    vcodec, acodec = ("none", "") if kind == "audio" else ("", "")
    codecs = re.search(r'codecs\s*=\s*["\']?([^;"\']+)', mime, flags=re.I)
    if codecs:
        for codec in [item.strip() for item in codecs[1].split(",")]:
            if codec.lower().startswith(("vp8", "vp9", "vp0", "av01", "avc", "theora")):
                vcodec = codec
            elif codec.lower().startswith(("opus", "vorbis", "mp4a", "aac", "mp3", "flac")):
                acodec = codec
    size = _number(info.get("size"), integer=True)
    bandwidth = _number(info.get("bandwidth"))
    bitrate = bandwidth / 1000 if bandwidth else (size * 8 / duration / 1000 if size and duration else None)
    return Format(url=url, format_id=format_id, ext=ext,
                  protocol="hls" if ext == "m3u8" else "dash" if ext == "mpd" else "http",
                  vcodec=vcodec, acodec=acodec,
                  width=_number(info.get("width"), integer=True),
                  height=_number(info.get("height"), integer=True),
                  fps=_number(info.get("framerate")), tbr=bitrate,
                  abr=bitrate if kind == "audio" else None, filesize=size,
                  headers=context.scoped_headers(url), metadata={"original": format_id == "original"})


def _item(page: dict, host: str, source: str, context: ExtractorContext) -> Media:
    records = page.get("videoinfo", page.get("imageinfo"))
    if not isinstance(records, list) or not records or not isinstance(records[0], dict):
        raise OmniError("Wikimedia file is missing, restricted, or has no downloadable original")
    info = records[0]
    if "filehidden" in info or "suppressed" in info:
        raise OmniError("Wikimedia file revision is hidden or restricted")
    url = _media_url(info.get("url"), host)
    mime = _mime(info.get("mime"))
    kind, ext = classify(url, mime)
    mediatype = info.get("mediatype")
    if mediatype in {"AUDIO", "VIDEO"}:
        kind = mediatype.lower()
    title = page.get("title")
    if not isinstance(title, str) or not title:
        raise OmniError("Wikimedia returned an invalid file title")
    title = title.split(":", 1)[-1]
    page_id = page.get("pageid")
    stable_id = str(page_id) if type(page_id) is int and page_id > 0 else hashlib.sha256(title.encode()).hexdigest()[:16]
    duration = _number(info.get("duration"))
    formats = []
    if kind in {"audio", "video"}:
        formats.append(_format(info, url, "original", kind, context, duration))
        seen = {url}
        seen_ids = {"original"}
        derivatives = info.get("derivatives", [])
        if not isinstance(derivatives, list):
            raise OmniError("Wikimedia returned invalid media renditions")
        for index, derivative in enumerate(derivatives):
            if not isinstance(derivative, dict):
                raise OmniError("Wikimedia returned an invalid media rendition")
            alternate = _media_url(derivative.get("src"), host)
            if alternate in seen:
                continue
            seen.add(alternate)
            key = derivative.get("transcodekey")
            if key is not None and not isinstance(key, str):
                raise OmniError("Wikimedia returned an invalid rendition identifier")
            raw_key = key or f"rendition-{index + 1}"
            safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", raw_key)[:80]
            if safe_key != raw_key:
                safe_key += "-" + hashlib.sha256(raw_key.encode()).hexdigest()[:10]
            format_id = "transcode-" + safe_key
            if format_id in seen_ids:
                format_id += "-" + hashlib.sha256(alternate.encode()).hexdigest()[:10]
            seen_ids.add(format_id)
            candidate = _format(derivative, alternate, format_id, kind, context, duration)
            candidate.metadata["transcode_key"] = raw_key
            formats.append(candidate)
    metadata = {"extractor": "wikimedia", "archive_id": f"wikimedia:{host}:{stable_id}",
                "webpage_url": "https://" + host + "/wiki/" + quote("File:" + title, safe=":"),
                "filesize": _number(info.get("size"), integer=True),
                "width": _number(info.get("width"), integer=True),
                "height": _number(info.get("height"), integer=True), "duration": duration}
    if isinstance(info.get("sha1"), str) and re.fullmatch(r"[0-9a-zA-Z]{31,40}", info["sha1"]):
        metadata["source_sha1"] = info["sha1"]
    subtitles = {}
    for subtitle in info.get("timedtext", []) if isinstance(info.get("timedtext", []), list) else []:
        if not isinstance(subtitle, dict) or not subtitle.get("src"):
            continue
        sub_url = _media_url(subtitle["src"], host, subtitle=True)
        language = str(subtitle.get("srclang") or "und")
        _, sub_ext = classify(sub_url, _mime(subtitle.get("type")))
        subtitles.setdefault(language, []).append({"url": sub_url, "ext": sub_ext or "vtt"})
    if subtitles:
        metadata["subtitles"] = subtitles
    return Media(url=url, title=PurePosixPath(title).stem, id=stable_id, kind=kind, ext=ext,
                 mime_type=mime, source_url=source, headers=context.scoped_headers(url),
                 metadata=metadata, formats=formats)


def _fetch_titles(host: str, titles: list[str], source: str, context: ExtractorContext) -> list[dict]:
    result = _query(host, context, titles="|".join(titles), redirects=1,
                    prop="videoinfo", viprop=_INFO_PROPS, vilimit=1)
    pages = _pages(result)
    if not any("videoinfo" in page for page in pages) and result.get("warnings"):
        # Some Wikimedia project installations lack TimedMediaHandler.
        result = _query(host, context, titles="|".join(titles), redirects=1,
                        prop="imageinfo", iiprop="url|size|mime|mediatype|sha1", iilimit=1)
        pages = _pages(result)
    aliases = {}
    query = result.get("query", {})
    for key in ("normalized", "redirects"):
        changes = query.get(key, []) if isinstance(query, dict) else []
        for change in changes if isinstance(changes, list) else []:
            if isinstance(change, dict) and isinstance(change.get("from"), str) and isinstance(change.get("to"), str):
                aliases[change["from"].replace("_", " ")] = change["to"].replace("_", " ")

    def canonical(title):
        title = title.replace("_", " ")
        seen = set()
        while title in aliases and aliases[title] != title:
            if title in seen:
                raise OmniError("Wikimedia returned cyclic file-page redirects")
            seen.add(title)
            title = aliases[title]
        return title

    positions = {}
    for index, title in enumerate(titles):
        positions.setdefault(canonical(title), index)
    actual_titles = [page.get("title") for page in pages]
    if not all(isinstance(title, str) for title in actual_titles):
        raise OmniError("Wikimedia returned invalid file-page titles")
    if set(actual_titles) != set(positions) or len(actual_titles) != len(set(actual_titles)):
        raise OmniError("Wikimedia returned an incomplete file-page batch")
    pages.sort(key=lambda page: positions[page["title"]])
    return pages


def extract(url: str, context: ExtractorContext) -> list[Media]:
    host, title = _source(url)
    if type(context.max_items) is not int or context.max_items < 1:
        raise OmniError("Wikimedia maximum items must be positive")
    if not title.startswith("Category:"):
        pages = _fetch_titles(host, [title], url, context)
        if len(pages) != 1:
            raise OmniError("Wikimedia returned an unexpected number of file pages")
        media = _item(pages[0], host, url, context)
        return [media] if not context.media_types or media.kind in context.media_types else []

    output, visited, seen_tokens, output_ids = [], set(), set(), set()
    continuation = None
    incomplete, skipped = False, 0
    while len(visited) < context.max_items:
        params = {"list": "categorymembers", "cmtitle": title, "cmtype": "file",
                  "cmlimit": min(50, context.max_items - len(visited))}
        if continuation:
            params["cmcontinue"] = continuation
        result = _query(host, context, **params)
        query = result.get("query")
        members = query.get("categorymembers") if isinstance(query, dict) else None
        if not isinstance(members, list) or not all(isinstance(member, dict) for member in members):
            raise OmniError("Wikimedia returned invalid category members")
        titles = []
        for member in members:
            candidate = member.get("title")
            if not isinstance(candidate, str) or ":" not in candidate or "|" in candidate or member.get("ns") != 6:
                raise OmniError("Wikimedia returned an invalid category file title")
            if candidate in visited:
                continue
            if len(visited) >= context.max_items:
                incomplete = True
                break
            visited.add(candidate)
            titles.append(candidate)
        if titles:
            for page in _fetch_titles(host, titles, url, context):
                try:
                    media = _item(page, host, url, context)
                except OmniError:
                    skipped += 1
                    continue
                if context.media_types and media.kind not in context.media_types:
                    continue
                if media.metadata["archive_id"] in output_ids:
                    continue
                output_ids.add(media.metadata["archive_id"])
                media.metadata.update(playlist_title=title, playlist_index=len(output) + 1)
                output.append(media)
        cont = result.get("continue", {})
        next_token = cont.get("cmcontinue") if isinstance(cont, dict) else None
        if not next_token:
            break
        if not isinstance(next_token, str) or next_token in seen_tokens or not titles:
            raise OmniError("Wikimedia category pagination did not advance")
        seen_tokens.add(next_token)
        continuation = next_token
        if len(visited) >= context.max_items:
            incomplete = True
    if skipped and not output:
        raise OmniError("Wikimedia category files were unavailable or restricted")
    for media in output:
        media.metadata.update(collection_truncated=incomplete, collection_skipped=skipped,
                              collection_complete=not incomplete and not skipped)
    return output
