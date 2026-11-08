"""Native, bounded discovery of heterogeneous media over HTTP(S).

This module parses static documents only. It neither executes page JavaScript nor
recursively crawls linked pages. Site-specific extraction belongs in plugins.
"""
from __future__ import annotations

import gzip
import hashlib
import http.client
import http.cookiejar
import json
import socket
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from email.message import Message
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urldefrag, urljoin, urlsplit

from . import __version__
from .models import Media, OmniError
from .transfer import request_url
from .types import DOCUMENT_MIMES, EXTENSION_KINDS, MANIFEST_MIMES, MEDIA_KINDS, classify, extension

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
_CROSS_ORIGIN_HEADERS = {"user-agent", "accept", "accept-language", "accept-encoding"}


def _url(value: str, base: str = "") -> str | None:
    try:
        resolved = urldefrag(urljoin(base, value.strip()))[0]
        parts = urlsplit(resolved)
        if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        # Force validation of malformed bracketed hosts and invalid ports.
        _ = parts.port
        if any(ord(c) < 32 or ord(c) == 127 for c in resolved):
            return None
        return resolved
    except (ValueError, TypeError, AttributeError):
        return None


def _origin(value: str) -> tuple[str, str | None, int | None]:
    p = urlsplit(value)
    hostname = p.hostname.encode("idna").decode("ascii").lower() if p.hostname else None
    return p.scheme.lower(), hostname, p.port or (443 if p.scheme == "https" else 80)


class _Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url(newurl, req.full_url):
            raise OmniError("Discovery redirect must use HTTP(S) without embedded credentials")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and _origin(req.full_url) != _origin(newurl):
            for key in list(redirected.headers):
                if key.lower() not in _CROSS_ORIGIN_HEADERS:
                    redirected.remove_header(key)
        return redirected


def _opener(cookies: str | None):
    handlers = [_Redirect()]
    if cookies:
        jar = http.cookiejar.MozillaCookieJar(cookies)
        try:
            jar.load(ignore_discard=True, ignore_expires=False)
        except (OSError, http.cookiejar.LoadError) as exc:
            raise OmniError("Cannot read Netscape-format cookie file") from exc
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    return urllib.request.build_opener(*handlers)


def _headers(headers: dict | None) -> dict[str, str]:
    result = {"User-Agent": f"omni-dlp/{__version__}", "Accept-Encoding": "identity"}
    for key, value in (headers or {}).items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or any(c in key + value for c in "\r\n"):
            raise OmniError("HTTP headers must be strings without newlines")
        # Make the explicit value replace defaults case-insensitively.
        result = {k: v for k, v in result.items() if k.lower() != key.lower()}
        result[key] = value
    return result


def _mime(headers) -> str:
    return headers.get("Content-Type", "").split(";", 1)[0].strip().lower()


def _filename(headers) -> str:
    value = headers.get("Content-Disposition", "")
    message = Message()
    message["Content-Disposition"] = value
    return message.get_filename() or ""


def _title(url: str, filename: str = "") -> str:
    name = PurePosixPath(filename or unquote(urlsplit(url).path)).name
    if not name:
        return urlsplit(url).hostname or "media"
    return name.rsplit(".", 1)[0] if "." in name else name


class _Collector:
    def __init__(self, source: str, headers: dict[str, str], media_types: set[str] | None, max_items: int):
        self.source = source
        self.base = source
        self.headers = headers
        self.media_types = media_types
        self.max_items = max_items
        self.items: dict[str, Media] = {}

    def add(self, value, *, title: str = "", mime: str = "", kind: str = "", filename: str = "", metadata: dict | None = None):
        if not isinstance(value, str):
            return
        url = _url(value, self.base)
        if not url or url in self.items or len(self.items) >= self.max_items:
            return
        guessed_kind, ext = classify(url, mime, filename)
        kind = kind if kind in MEDIA_KINDS else guessed_kind
        if self.media_types and kind not in self.media_types:
            return
        safe_headers = dict(self.headers)
        if _origin(self.source) != _origin(url):
            safe_headers = {key: val for key, val in safe_headers.items() if key.lower() in _CROSS_ORIGIN_HEADERS}
        meta = dict(metadata or {})
        if ext in {"m3u8", "mpd"}:
            meta["protocol"] = "hls" if ext == "m3u8" else "dash"
        self.items[url] = Media(
            url=url, title=title.strip() or _title(url, filename),
            id=hashlib.sha256(url.encode()).hexdigest()[:16], kind=kind,
            ext=ext, mime_type=mime, source_url=self.source,
            headers=safe_headers, metadata=meta,
        )


def _srcset(value: str):
    # HTTP URLs cannot contain literal spaces; descriptors are separated by them.
    # Embedded data URIs are deliberately excluded.
    for part in value.split(","):
        candidate = part.strip().split()
        if candidate and not candidate[0].startswith("data:"):
            yield candidate[0]


class _Page(HTMLParser):
    def __init__(self, collector: _Collector):
        super().__init__(convert_charrefs=True)
        self.collector = collector
        self.title = ""
        self.in_title = False
        self.stack: list[str] = []
        self.in_json = False
        self.json_chunks: list[str] = []
        self.json_documents: list[str] = []
        self.base_seen = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag in {"audio", "video", "picture"}:
            self.stack.append(tag)
        elif tag == "base" and not self.base_seen:
            candidate = _url(a.get("href", ""), self.collector.source)
            if candidate:
                self.collector.base = candidate
                self.base_seen = True
        elif tag == "script" and (a.get("type") or "").split(";", 1)[0].lower() == "application/ld+json":
            self.in_json = True
            self.json_chunks = []
        kind = ""
        keys: tuple[str, ...] = ()
        if tag == "img":
            kind, keys = "image", ("src", "data-src", "data-original", "data-lazy-src")
        elif tag in {"audio", "video"}:
            kind, keys = tag, ("src",)
        elif tag == "source":
            kind = next(("image" if item == "picture" else item for item in reversed(self.stack)), "")
            keys = ("src",)
        elif tag == "track":
            kind, keys = "subtitle", ("src",)
        elif tag in {"a", "link"}:
            href = a.get("href") or ""
            mime = a.get("type") or ""
            guessed_kind, ext = classify(href, mime)
            if ext in EXTENSION_KINDS or mime.split("/", 1)[0] in {"image", "audio", "video", "font", "model"} or "download" in a or "enclosure" in (a.get("rel") or "").split():
                keys = ("href",)
                kind = guessed_kind
        elif tag in {"object", "embed"}:
            keys = ("data",) if tag == "object" else ("src",)
        if tag == "video" and a.get("poster"):
            self.collector.add(a["poster"], kind="image", title=a.get("title") or "")
        for key in keys:
            self.collector.add(a.get(key), title=a.get("alt") or a.get("title") or "", mime=a.get("type") or "", kind=kind)
        if tag in {"img", "source"}:
            for key in ("srcset", "data-srcset"):
                for candidate in _srcset(a.get(key) or ""):
                    self.collector.add(candidate, kind=kind or "image", title=a.get("alt") or "", mime=a.get("type") or "")
        if tag == "meta":
            prop = (a.get("property") or a.get("name") or "").lower()
            mapping = {"og:image": "image", "og:image:url": "image", "og:image:secure_url": "image", "twitter:image": "image", "twitter:image:src": "image", "og:audio": "audio", "og:audio:url": "audio", "og:audio:secure_url": "audio", "og:video": "video", "og:video:url": "video", "og:video:secure_url": "video", "twitter:player:stream": "video"}
            if prop in mapping:
                self.collector.add(a.get("content"), kind=mapping[prop])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            self.stack = self.stack[:index]
        if tag == "script" and self.in_json:
            self.json_documents.append("".join(self.json_chunks))
            self.in_json = False

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        if self.in_json:
            self.json_chunks.append(data)


def _jsonld(value, collector: _Collector, depth: int = 0):
    if depth > 40:
        return
    if isinstance(value, list):
        for child in value:
            _jsonld(child, collector, depth + 1)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        types = [types] if isinstance(types, str) else types
        types = [str(t).rsplit("/", 1)[-1] for t in types] if isinstance(types, list) else []
        kind = next((k for t, k in (("ImageObject", "image"), ("AudioObject", "audio"), ("VideoObject", "video")) if t in types), "")
        mime = value.get("encodingFormat") or ""
        mime = mime if isinstance(mime, str) and "/" in mime else ""
        title = value.get("name") or value.get("headline") or ""
        title = title if isinstance(title, str) else ""
        if kind or "MediaObject" in types:
            urls = value.get("contentUrl") or value.get("url") or []
            for url in urls if isinstance(urls, list) else [urls]:
                collector.add(url, title=title, mime=mime, kind=kind)
        else:
            collector.add(value.get("contentUrl"), title=title, mime=mime)
        images = value.get("image", [])
        for item in images if isinstance(images, list) else [images]:
            if isinstance(item, str):
                collector.add(item, kind="image", title=title)
        for child in value.values():
            if isinstance(child, (dict, list)):
                _jsonld(child, collector, depth + 1)


class _FeedTreeBuilder(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # Parser callbacks run after encoding detection and before entity use.
        # Raw byte searches miss UTF-16 declarations and must not enforce this.
        raise OmniError("Feed contains unsupported XML declarations")


def _feed(data: bytes, collector: _Collector):
    try:
        root = ET.fromstring(data, parser=ET.XMLParser(target=_FeedTreeBuilder()))
    except ET.ParseError as exc:
        raise OmniError("Invalid XML feed") from exc
    def tag(node):
        return node.tag.rsplit("}", 1)[-1].lower()
    if tag(root) not in {"rss", "feed", "rdf"}:
        raise OmniError("XML document is not an RSS or Atom feed")
    entries = [item for item in root.iter() if tag(item) in {"item", "entry"}]
    for entry in entries:
        title = next(("".join(c.itertext()).strip() for c in entry if tag(c) == "title"), "")
        for node in entry.iter():
            name = tag(node)
            mime = node.get("type", "")
            if name == "enclosure" or (name == "link" and node.get("rel") == "enclosure"):
                collector.add(node.get("url") or node.get("href"), title=title, mime=mime)
            elif name in {"content", "thumbnail"} and node.get("url"):
                collector.add(node.get("url"), title=title, mime=mime, kind="image" if name == "thumbnail" else node.get("medium", ""))
            elif name == "image" and node.get("href"):
                collector.add(node.get("href"), title=title, kind="image")


def _manifest(value, collector: _Collector):
    if isinstance(value, dict) and "@context" in value:
        _jsonld(value, collector)
        return
    if isinstance(value, dict) and isinstance(value.get("version"), str) and value["version"].startswith("https://jsonfeed.org/version/"):
        entries = value.get("items", [])
        if not isinstance(entries, list):
            raise OmniError("JSON Feed items must be an array")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            attachments = entry.get("attachments") or []
            if not isinstance(attachments, list):
                raise OmniError("JSON Feed attachments must be an array")
            for attachment in attachments:
                if isinstance(attachment, dict):
                    title = attachment.get("title") or entry.get("title") or ""
                    mime = attachment.get("mime_type") or ""
                    if not isinstance(title, str) or not isinstance(mime, str):
                        raise OmniError("JSON Feed attachment titles and MIME types must be strings")
                    collector.add(attachment.get("url"), title=title, mime=mime)
        return
    entries = value.get("media", value.get("items", [])) if isinstance(value, dict) else value
    if not isinstance(entries, list):
        raise OmniError("Media manifest must contain a media array")
    for entry in entries:
        if isinstance(entry, str):
            collector.add(entry)
        elif isinstance(entry, dict):
            title, mime, kind = (entry.get(key) or "" for key in ("title", "mime_type", "kind"))
            if not all(isinstance(x, str) for x in (title, mime, kind)):
                raise OmniError("Manifest title, mime_type, and kind must be strings")
            collector.add(entry.get("url"), title=title, mime=mime, kind=kind)


def _is_direct(url: str, headers) -> bool:
    mime = _mime(headers)
    if mime in MANIFEST_MIMES:
        return True
    if mime in {"text/html", "application/xhtml+xml"}:
        return False
    if extension(_filename(headers) or url) in EXTENSION_KINDS:
        return True
    if mime in DOCUMENT_MIMES:
        return False
    if _filename(headers):
        return True
    return bool(mime and mime not in {"text/plain", "application/octet-stream", "binary/octet-stream"}) or mime in {"application/octet-stream", "binary/octet-stream"}


def discover(url: str, *, headers: dict | None = None, cookies: str | None = None, timeout: float = 30, media_types: set[str] | None = None, max_items: int = 1000) -> list[Media]:
    """Discover direct files or media in static HTML, feeds, or JSON manifests.

    JSON manifests are ``{"media": [{"url": "relative/or/absolute",
    "title": "optional", "kind": "optional", "mime_type": "optional"}]}``.
    Direct unknown binary formats are returned with kind ``other``. Cookies use
    Netscape cookie-file format. Limits apply before transfer begins.
    """
    clean_url = _url(url)
    if not clean_url:
        raise OmniError("Provide an HTTP(S) URL without embedded credentials")
    if timeout <= 0 or max_items < 1:
        raise OmniError("timeout and max_items must be positive")
    if media_types and not set(media_types).issubset(MEDIA_KINDS):
        raise OmniError("Unknown media type; choose from " + ", ".join(sorted(MEDIA_KINDS)))
    request_headers = _headers(headers)
    opener = _opener(cookies)
    collector = _Collector(clean_url, request_headers, media_types, max_items)
    try:
        try:
            response = opener.open(urllib.request.Request(request_url(clean_url), headers=request_headers, method="HEAD"), timeout=timeout)
        except urllib.error.HTTPError as exc:
            # Many origin servers reject HEAD despite supporting GET.
            if exc.code not in {400, 403, 405, 501}:
                raise
            response = None
            exc.close()
        if response is not None:
            with response:
                final_url = response.geturl()
                if _is_direct(final_url, response.headers):
                    collector.add(final_url, mime=_mime(response.headers), filename=_filename(response.headers))
                    return list(collector.items.values())
        with opener.open(urllib.request.Request(request_url(clean_url), headers=request_headers), timeout=timeout) as response:
            final_url = response.geturl()
            collector.base = final_url
            if _is_direct(final_url, response.headers):
                collector.add(final_url, mime=_mime(response.headers), filename=_filename(response.headers))
                return list(collector.items.values())
            stream = gzip.GzipFile(fileobj=response) if response.headers.get("Content-Encoding", "").lower() == "gzip" else response
            mime = _mime(response.headers)
            prefix = stream.read(512)
            sniff = prefix.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
            looks_like_document = mime in DOCUMENT_MIMES or sniff.startswith((b"{", b"[", b"<?xml", b"<rss", b"<feed", b"<!doctype html", b"<html", b"<head", b"<body"))
            if not looks_like_document:
                collector.add(final_url, mime=mime)
                return list(collector.items.values())
            data = prefix + stream.read(MAX_DOCUMENT_BYTES + 1 - len(prefix))
            if len(data) > MAX_DOCUMENT_BYTES:
                raise OmniError("Discovery document exceeds 4 MiB limit")
            charset = response.headers.get_content_charset() or "utf-8"
            try:
                decoded = data.decode(charset, errors="replace")
            except LookupError:
                decoded = data.decode("utf-8", errors="replace")
        stripped = decoded.lstrip("\ufeff \t\r\n")
        if mime in {"application/json", "application/feed+json"} or stripped.startswith(("{", "[")):
            try:
                _manifest(json.loads(stripped), collector)
            except (ValueError, RecursionError) as exc:
                raise OmniError("Invalid JSON media manifest") from exc
        elif mime in {"application/rss+xml", "application/atom+xml", "text/xml", "application/xml"} or (mime not in {"text/html", "application/xhtml+xml"} and stripped.startswith(("<?xml", "<rss", "<feed"))):
            _feed(data, collector)
        elif mime in {"text/html", "application/xhtml+xml"} or stripped.lower().startswith(("<!doctype html", "<html", "<head", "<body")):
            page = _Page(collector)
            page.feed(decoded)
            page.close()
            for document in page.json_documents:
                try:
                    _jsonld(json.loads(document), collector)
                except (ValueError, RecursionError):
                    continue  # Malformed unrelated JSON-LD must not hide HTML media.
        else:
            # A text document or unknown binary is itself downloadable.
            collector.add(final_url, mime=mime)
    except OmniError:
        raise
    except urllib.error.HTTPError as exc:
        raise OmniError(f"Discovery request failed with HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, socket.timeout, OSError, ValueError, http.client.HTTPException, EOFError) as exc:
        raise OmniError("Discovery request failed; check the URL, network, and authentication") from exc
    return list(collector.items.values())
