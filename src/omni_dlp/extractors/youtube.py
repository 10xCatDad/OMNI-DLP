"""Native YouTube player-response extraction, without upstream execution.

Only directly usable HTTP(S) formats and clear manifests are returned. Encrypted
signature, throttling, proof-of-origin and SABR protocols are not implemented.
The extractor never evaluates scripts supplied by a remote page.
"""
from __future__ import annotations

from dataclasses import replace
from html import unescape
from html.parser import HTMLParser
import json
import math
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import Format, Media, OmniError
from ..transfer import validate_url
from .common import ExtractorContext

_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com",
          "youtube-nocookie.com", "www.youtube-nocookie.com", "youtu.be", "www.youtu.be"}
_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_SAFE_STATUS = {"ERROR", "UNPLAYABLE", "LOGIN_REQUIRED", "AGE_CHECK_REQUIRED",
                "CONTENT_CHECK_REQUIRED", "LIVE_STREAM_OFFLINE"}
_AUDIO_CODECS = ("mp4a", "opus", "vorbis", "aac", "flac", "ac-3", "ec-3", "dts")


def video_id(url: str) -> str | None:
    """Recognize supported single-video URLs, excluding host lookalikes."""
    try:
        validate_url(url)
        parsed = urlsplit(url)
        if parsed.hostname not in _HOSTS or parsed.port not in {None, 80, 443}:
            return None
        if parsed.hostname in {"youtu.be", "www.youtu.be"}:
            candidate = parsed.path.strip("/")
        elif parsed.path.rstrip("/") == "/watch":
            values = parse_qs(parsed.query, max_num_fields=128).get("v", [])
            candidate = values[0] if len(values) == 1 else ""
        else:
            match = re.fullmatch(r"/(?:embed|shorts|live|v)/([A-Za-z0-9_-]{11})/?", parsed.path)
            candidate = match[1] if match else ""
        return candidate if _ID.fullmatch(candidate) else None
    except (OmniError, ValueError):
        return None


def supports(url: str) -> bool:
    return video_id(url) is not None


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts: list[tuple[str, str]] = []
        self.current: list[str] | None = None
        self.identifier = ""

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.current = []
            self.identifier = dict(attrs).get("id", "")

    def handle_data(self, data):
        if self.current is not None:
            self.current.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.current is not None:
            self.scripts.append((self.identifier, "".join(self.current)))
            self.current = None


def _embedded_json(scripts: list[tuple[str, str]], name: str) -> list[dict]:
    # JSONDecoder correctly handles braces and semicolons inside JSON strings.
    # This does not execute JavaScript or accept arbitrary JavaScript objects.
    marker = re.compile(r"(?<![\w$])(?:" + re.escape(name)
                        + r"|(?:window|self)\s*\[\s*['\"]" + re.escape(name)
                        + r"['\"]\s*\])\s*=\s*")
    decoder = json.JSONDecoder()
    result = []
    for identifier, script in scripts:
        positions = [0] if identifier == name else []
        positions.extend(match.end() for match in marker.finditer(script))
        for position in positions[:100]:
            text = script[position:].lstrip()
            try:
                if text.startswith("JSON.parse("):
                    literal, _ = decoder.raw_decode(text[len("JSON.parse("):].lstrip())
                    value = json.loads(literal) if isinstance(literal, str) else None
                else:
                    value, _ = decoder.raw_decode(text)
                if isinstance(value, dict):
                    result.append(value)
            except (ValueError, TypeError, RecursionError):
                continue
    return result


def _player(scripts: list[tuple[str, str]], identifier: str) -> dict:
    responses = _embedded_json(scripts, "ytInitialPlayerResponse")
    # Older embedded players wrap the same JSON response in their config args.
    for config in _embedded_json(scripts, "ytplayer.config"):
        args = config.get("args")
        raw = args.get("player_response") if isinstance(args, dict) else None
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    responses.append(parsed)
            except (ValueError, RecursionError):
                continue
    matches = [value for value in responses if isinstance(value.get("videoDetails"), dict)
               and value["videoDetails"].get("videoId") == identifier]
    if matches:
        return next((value for value in matches if value.get("streamingData")), matches[0])
    # A denied request may omit videoDetails, but cannot produce a media item.
    for value in responses:
        status = value.get("playabilityStatus")
        if isinstance(status, dict) and status.get("status") in _SAFE_STATUS:
            if not value.get("videoDetails"):
                return value
    if responses:
        raise OmniError("YouTube player response does not match the requested video identity")
    raise OmniError("YouTube page has no supported player response; consent, login, or a changed page may require another extraction path")


def _number(value, *, integer=False, positive=False):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0 or (positive and number <= 0):
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def _text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if isinstance(value.get("simpleText"), str):
            return value["simpleText"]
        runs = value.get("runs")
        if isinstance(runs, list):
            return "".join(run.get("text", "") for run in runs if isinstance(run, dict) and isinstance(run.get("text"), str))
    return ""


def _url(value) -> str | None:
    if not isinstance(value, str):
        return None
    value = unescape(value)
    try:
        validate_url(value)
    except OmniError:
        return None
    return value


def _query(url: str, **values: str) -> str:
    parsed = urlsplit(url)
    fields = [(name, value) for name, value in parse_qsl(parsed.query, keep_blank_values=True, max_num_fields=256)
              if name not in values]
    fields.extend(values.items())
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(fields), parsed.fragment))


def _challenge(url: str) -> bool:
    parsed = urlsplit(url)
    return bool(parse_qs(parsed.query, max_num_fields=256).get("n")) or bool(re.search(r"/n/[^/]+", parsed.path))


def _format(raw: dict, context: ExtractorContext, omitted: dict[str, int]) -> Format | None:
    def skip(reason):
        omitted[reason] = omitted.get(reason, 0) + 1
        return None

    if raw.get("drmFamilies") or raw.get("drmTrackType") or raw.get("licenseInfos"):
        return skip("drm")
    url = _url(raw.get("url"))
    if not url:
        cipher = raw.get("signatureCipher") or raw.get("cipher")
        if isinstance(cipher, str):
            try:
                fields = parse_qs(cipher, keep_blank_values=True, max_num_fields=32)
                if fields.get("s"):
                    return skip("encrypted_signature")
                url = _url(fields.get("url", [None])[0])
                signature = (fields.get("sig") or fields.get("signature") or [None])[0]
                name = fields.get("sp", ["signature"])[0]
                if url and signature:
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
                        return skip("invalid_signature_parameter")
                    url = _query(url, **{name: signature})
            except ValueError:
                return skip("invalid_cipher")
        if not url:
            return skip("missing_url")
    try:
        if _challenge(url):
            return skip("throttling_challenge")
    except ValueError:
        return skip("invalid_url_query")
    mime = raw.get("mimeType", "")
    if not isinstance(mime, str):
        return skip("invalid_mime_type")
    primary = mime.split(";", 1)[0].strip().lower()
    if primary not in {"audio/mp4", "video/mp4", "audio/webm", "video/webm", "audio/3gpp", "video/3gpp", "audio/ogg"}:
        return skip("unknown_media_type")
    match = re.search(r'codecs\s*=\s*["\']([^"\']+)', mime, re.I)
    codecs = [codec.strip() for codec in match[1].split(",")] if match else []
    audio = next((codec for codec in codecs if codec.lower().startswith(_AUDIO_CODECS)), "")
    video = next((codec for codec in codecs if not codec.lower().startswith(_AUDIO_CODECS)), "")
    if primary.startswith("audio/"):
        video = "none"
        audio = audio or (codecs[0] if codecs else "unknown")
    else:
        video = video or "unknown"
        audio = audio or ("unknown" if raw.get("audioQuality") or raw.get("audioChannels") else "none")
    ext = {"audio/mp4": "m4a", "video/mp4": "mp4", "audio/webm": "webm", "video/webm": "webm",
           "audio/3gpp": "3gp", "video/3gpp": "3gp", "audio/ogg": "ogg"}[primary]
    identifier = str(raw.get("itag", ""))
    if not re.fullmatch(r"[0-9]{1,8}", identifier):
        return skip("missing_format_id")
    track = raw.get("audioTrack") if isinstance(raw.get("audioTrack"), dict) else {}
    track_id = track.get("id", "") if isinstance(track.get("id", ""), str) else ""
    if track_id:
        identifier += "-" + track_id[:64]
    if raw.get("isDrc"):
        identifier += "-drc"
    bitrate = _number(raw.get("bitrate") or raw.get("averageBitrate"))
    metadata = {key: raw[key] for key in ("quality", "qualityLabel", "audioQuality", "audioSampleRate", "audioChannels") if key in raw}
    if track:
        metadata["audio_is_default"] = bool(track.get("audioIsDefault"))
        metadata["audio_track_name"] = _text(track.get("displayName"))
    return Format(url=url, format_id=identifier, ext=ext, protocol="http", vcodec=video, acodec=audio,
                  width=_number(raw.get("width"), integer=True, positive=True),
                  height=_number(raw.get("height"), integer=True, positive=True),
                  fps=_number(raw.get("fps"), positive=True),
                  filesize=_number(raw.get("contentLength"), integer=True),
                  tbr=bitrate / 1000 if bitrate is not None else None,
                  abr=bitrate / 1000 if bitrate is not None and video == "none" else None,
                  language=track_id.split(".")[0], headers=context.scoped_headers(url), metadata=metadata)


def _walk(value, *, maximum=10000):
    pending = [(value, 0)]
    visited = 0
    while pending and visited < maximum:
        current, depth = pending.pop()
        visited += 1
        if depth > 64:
            continue
        if isinstance(current, dict):
            yield current
            pending.extend((child, depth + 1) for child in current.values() if isinstance(child, (dict, list)))
        elif isinstance(current, list):
            pending.extend((child, depth + 1) for child in current if isinstance(child, (dict, list)))


def _chapters(initial: dict, duration: float | None) -> list[dict]:
    chapters = {}
    overlays = initial.get("playerOverlays", {})
    for node in _walk(overlays):
        chapter = node.get("chapterRenderer")
        if not isinstance(chapter, dict):
            continue
        start_ms = _number(chapter.get("timeRangeStartMillis"))
        title = _text(chapter.get("title"))
        if start_ms is not None and title and (duration is None or start_ms / 1000 < duration):
            chapters[start_ms / 1000] = title
    starts = sorted(chapters)
    return [{"start_time": start, "end_time": starts[index + 1] if index + 1 < len(starts) else duration,
             "title": chapters[start]} for index, start in enumerate(starts)]


def _captions(response: dict) -> tuple[dict, dict]:
    container = response.get("captions", {})
    container = container.get("playerCaptionsTracklistRenderer", {}) if isinstance(container, dict) else {}
    tracks = container.get("captionTracks", []) if isinstance(container, dict) else []
    manual: dict[str, list] = {}
    automatic: dict[str, list] = {}
    for track in tracks[:2000] if isinstance(tracks, list) else []:
        if not isinstance(track, dict):
            continue
        url = _url(track.get("baseUrl"))
        language = track.get("languageCode")
        if not url or not isinstance(language, str) or not language or len(language) > 128:
            continue
        try:
            url = _query(url, fmt="vtt")
        except ValueError:
            continue
        target = automatic if track.get("kind") == "asr" else manual
        item = {"url": url, "ext": "vtt", "name": _text(track.get("name"))}
        if item not in target.setdefault(language, []):
            target[language].append(item)
    return manual, automatic


def extract(url: str, context: ExtractorContext) -> list[Media]:
    identifier = video_id(url)
    if not identifier:
        raise OmniError("Unsupported YouTube single-video URL")
    canonical = "https://www.youtube.com/watch?v=" + identifier
    page = context.get_text(canonical + "&hl=en")
    parser = _Scripts()
    parser.feed(page)
    response = _player(parser.scripts, identifier)
    playability = response.get("playabilityStatus", {})
    status = playability.get("status", "") if isinstance(playability, dict) else ""
    if status and status != "OK":
        label = status if status in _SAFE_STATUS else "unavailable"
        raise OmniError(f"YouTube playback is {label}; the native extractor does not override access restrictions")
    details = response.get("videoDetails", {})
    if not isinstance(details, dict) or details.get("videoId") != identifier:
        raise OmniError("YouTube player response is missing the requested video identity")
    micro = response.get("microformat", {})
    micro = micro.get("playerMicroformatRenderer", {}) if isinstance(micro, dict) else {}
    if not isinstance(micro, dict):
        micro = {}
    title = _text(details.get("title")) or identifier
    duration = _number(details.get("lengthSeconds"))
    thumbnails = []
    thumbnail_data = details.get("thumbnail", {})
    for item in thumbnail_data.get("thumbnails", [])[:2000] if isinstance(thumbnail_data, dict) and isinstance(thumbnail_data.get("thumbnails", []), list) else []:
        if isinstance(item, dict) and (thumbnail := _url(item.get("url"))):
            thumbnails.append({"url": thumbnail, "width": _number(item.get("width"), integer=True),
                               "height": _number(item.get("height"), integer=True)})
    manual, automatic = _captions(response)
    live = micro.get("liveBroadcastDetails", {})
    is_live = bool(details.get("isLive") or (live.get("isLiveNow") if isinstance(live, dict) else False))
    upload_date = _text(micro.get("uploadDate"))
    upload_date = upload_date.replace("-", "") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", upload_date) else ""
    metadata = {"extractor": "youtube", "extractor_key": "YouTube", "archive_id": "youtube:" + identifier,
                "webpage_url": canonical, "description": _text(details.get("shortDescription")),
                "uploader": _text(details.get("author")), "channel_id": _text(details.get("channelId")),
                "duration": duration, "view_count": _number(details.get("viewCount"), integer=True),
                "is_live": is_live, "was_live": bool(details.get("isLiveContent")) and not is_live,
                "upload_date": upload_date, "thumbnails": thumbnails,
                "subtitles": manual, "automatic_captions": automatic}
    initial = _embedded_json(parser.scripts, "ytInitialData")
    metadata["chapters"] = _chapters(initial[0], duration) if initial else []
    base = Media(url=canonical, title=title, id=identifier, kind="video", source_url=context.source_url, metadata=metadata)
    kinds = context.media_types
    assets = []
    if kinds and "image" in kinds:
        for index, item in enumerate(thumbnails, 1):
            path = urlsplit(item["url"]).path.lower()
            ext = "webp" if path.endswith(".webp") else "png" if path.endswith(".png") else "jpg"
            assets.append(replace(base, url=item["url"], id=f"{identifier}.thumbnail.{index}", kind="image", ext=ext,
                                  headers=context.scoped_headers(item["url"]),
                                  metadata={**metadata, "archive_id": f"youtube:{identifier}:thumbnail:{index}",
                                            "asset_role": "thumbnail", "width": item["width"], "height": item["height"]}))
    if kinds and "subtitle" in kinds:
        for mode, groups in (("manual", manual), ("automatic", automatic)):
            for language, tracks in groups.items():
                for index, item in enumerate(tracks, 1):
                    assets.append(replace(base, url=item["url"], id=f"{identifier}.{language}.{mode}.{index}",
                                          kind="subtitle", ext="vtt", title=f"{title} [{language}]",
                                          headers=context.scoped_headers(item["url"]),
                                          metadata={**metadata, "archive_id": f"youtube:{identifier}:caption:{language}:{mode}:{index}",
                                                    "asset_role": "caption", "language": language, "caption_kind": mode}))
    if kinds and not kinds.intersection({"video", "audio"}):
        return assets[:context.max_items]
    data = response.get("streamingData", {})
    data = data if isinstance(data, dict) else {}
    omitted: dict[str, int] = {}
    formats = []
    seen = set()
    for key in ("formats", "adaptiveFormats"):
        entries = data.get(key, [])
        for raw in entries[:2000] if isinstance(entries, list) else []:
            if not isinstance(raw, dict):
                continue
            candidate = _format(raw, context, omitted)
            if candidate and (candidate.format_id, candidate.url) not in seen:
                seen.add((candidate.format_id, candidate.url))
                formats.append(candidate)
    for key, protocol, ext in (("hlsManifestUrl", "hls", "m3u8"), ("dashManifestUrl", "dash", "mpd")):
        manifest = _url(data.get(key))
        if not manifest:
            continue
        try:
            if _challenge(manifest):
                omitted["throttling_challenge"] = omitted.get("throttling_challenge", 0) + 1
                continue
        except ValueError:
            continue
        formats.append(Format(url=manifest, format_id=protocol, ext=ext, protocol=protocol,
                              vcodec="unknown", acodec="unknown", headers=context.scoped_headers(manifest),
                              metadata={"manifest": True}))
    if data.get("serverAbrStreamingUrl"):
        omitted["sabr_endpoint"] = 1
    if kinds and "audio" in kinds and "video" not in kinds:
        formats = [item for item in formats if item.vcodec == "none" and item.acodec != "none"]
        base = replace(base, kind="audio")
    if not formats:
        if assets:
            return assets[:context.max_items]
        reasons = ", ".join(f"{key}={value}" for key, value in sorted(omitted.items())) or "no direct formats"
        raise OmniError("YouTube supplied no independently usable formats (" + reasons
                        + "). Native signature/n challenges, PO-token generation, and SABR are not implemented")
    metadata["omitted_formats"] = omitted
    first = formats[0]
    return [replace(base, url=first.url, ext=first.ext, headers=first.headers, formats=formats), *assets][:context.max_items]
