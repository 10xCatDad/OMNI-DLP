from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest

from omni_dlp.discovery import MAX_DOCUMENT_BYTES, discover
from omni_dlp.models import OmniError
from omni_dlp.types import classify


@contextmanager
def server(routes):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.serve(False)

        def do_GET(self):
            self.serve(True)

        def serve(self, body):
            seen.append((self.command, self.path, dict(self.headers)))
            route = routes.get(self.path, (404, "text/plain", b"missing"))
            status, mime, payload, *extras = route
            headers = extras[0] if extras else {}
            if self.command == "HEAD" and headers.get("X-Reject-Head"):
                status = 405
            self.send_response(status)
            if mime:
                self.send_header("Content-Type", mime)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if body:
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", seen
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_direct_mime_without_extension_only_uses_head():
    with server({"/media": (200, "image/jpeg", b"large image")}) as (base, seen):
        [item] = discover(base + "/media")
        assert (item.kind, item.ext, item.mime_type) == ("image", "jpg", "image/jpeg")
        assert [request[0] for request in seen] == ["HEAD"]


def test_unknown_binary_is_downloadable_and_content_disposition_names_it():
    with server({"/binary": (200, "application/octet-stream", b"data", {"Content-Disposition": 'attachment; filename="dataset.custom"'})}) as (base, seen):
        [item] = discover(base + "/binary")
        assert item.kind == "other"
        assert item.ext == "custom"
        assert item.title == "dataset"
        assert len(seen) == 1


def test_html_mixed_media_base_dedup_and_srcset():
    html = b'''<html><head><base href="/assets/"><meta property="og:image" content="cover.jpg"></head>
    <body><img src="cover.jpg#one" alt="Cover"><img data-src="lazy.avif">
    <picture><source srcset="small.webp 1x, large.webp 2x" type="image/webp"></picture>
    <audio src="podcast.mp3"></audio><video src="movie.mp4" poster="poster.png">
    <track src="captions.vtt"></video><a href="book.pdf">Book</a><a href="/next">Next</a>
    <img src="data:image/png;base64,AAA"><a href="javascript:alert(1)">Bad</a></body></html>'''
    with server({"/page": (200, "text/html", html)}) as (base, _):
        items = discover(base + "/page")
        assert {item.url for item in items} == {base + "/assets/" + file for file in ["cover.jpg", "lazy.avif", "small.webp", "large.webp", "podcast.mp3", "movie.mp4", "poster.png", "captions.vtt", "book.pdf"]}
        kinds = {item.kind for item in items}
        assert kinds == {"image", "audio", "video", "subtitle", "document"}


def test_jsonld_nested_graph_and_malformed_script():
    html = b'''<html><script type="application/ld+json">broken</script><script type="application/ld+json">
    {"@context":"https://schema.org", "@graph":[{"@type":"VideoObject", "name":"Clip", "contentUrl":"clip.mp4"},
    {"@type":"ImageObject", "contentUrl":["one.jpg","two.jpg"]}, {"@type":"Article", "image":"article.webp"}]}
    </script></html>'''
    with server({"/": (200, "text/html", html)}) as (base, _):
        items = discover(base + "/")
        assert len(items) == 4
        assert items[0].title == "Clip"
        assert items[0].kind == "video"
        assert all(x.kind == "image" for x in items[1:])


@pytest.mark.parametrize("payload,mime", [
    (b'<rss><channel><item><title>Episode</title><enclosure url="episode.mp3" type="audio/mpeg"/></item></channel></rss>', "application/rss+xml"),
    (b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Episode</title><link rel="enclosure" href="episode.mp3" type="audio/mpeg"/></entry></feed>', "application/atom+xml"),
    (json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [{"title": "Episode", "attachments": [{"url": "episode.mp3", "mime_type": "audio/mpeg"}]}]}).encode(), "application/feed+json"),
])
def test_feed_enclosures(payload, mime):
    with server({"/feed": (200, mime, payload)}) as (base, _):
        [item] = discover(base + "/feed")
        assert item.url == base + "/episode.mp3"
        assert item.title == "Episode"
        assert item.kind == "audio"


def test_manifest_types_relative_urls_filter_and_limit():
    payload = json.dumps({"media": [{"url": "one.jpg", "title": "One"}, {"url": "two.mp3"}, {"url": "three.png"}, {"url": "four.png"}]}).encode()
    with server({"/manifest": (200, "application/json", payload)}) as (base, _):
        items = discover(base + "/manifest", media_types={"image"}, max_items=2)
        assert [x.url for x in items] == [base + "/one.jpg", base + "/three.png"]
        assert items[0].title == "One"


@pytest.mark.parametrize("path,mime,protocol", [("/live.m3u8", "application/vnd.apple.mpegurl", "hls"), ("/stream", "application/dash+xml", "dash")])
def test_adaptive_manifest_is_media_not_a_feed(path, mime, protocol):
    with server({path: (200, mime, b"manifest")}) as (base, seen):
        [item] = discover(base + path)
        assert item.metadata["protocol"] == protocol
        assert item.kind == "video"
        assert len(seen) == 1


def test_head_not_supported_falls_back_to_get():
    with server({"/image": (200, "image/png", b"not read", {"X-Reject-Head": "yes"})}) as (base, seen):
        [item] = discover(base + "/image")
        assert item.ext == "png"
        assert [x[0] for x in seen] == ["HEAD", "GET"]


def test_document_size_limit():
    with server({"/huge": (200, "text/html", b"x" * (MAX_DOCUMENT_BYTES + 1))}) as (base, _):
        with pytest.raises(OmniError, match="4 MiB"):
            discover(base + "/huge")


def test_errors_do_not_expose_url_query_credentials():
    with server({}) as (base, _):
        with pytest.raises(OmniError) as exc:
            discover(base + "/missing?token=secret")
        assert "secret" not in str(exc.value)
        assert "404" in str(exc.value)


def test_rejects_non_http_credentials_headers_and_limits():
    for url in ["file:///etc/passwd", "ftp://example.org/a", "http://user:pass@example.org/a", "https://example.org:bad/a"]:
        with pytest.raises(OmniError):
            discover(url)
    with pytest.raises(OmniError, match="newlines"):
        discover("https://example.org", headers={"X-Foo": "bad\r\nvalue"})
    with pytest.raises(OmniError, match="positive"):
        discover("https://example.org", max_items=0)


def test_redirect_and_html_assets_do_not_leak_sensitive_headers():
    with server({"/file.jpg": (200, "image/jpeg", b"x")}) as (other, other_seen):
        html = f'<html><img src="{other}/file.jpg"><img src="local.jpg"></html>'.encode()
        with server({"/page": (200, "text/html", html), "/redirect": (302, "", b"", {"Location": other + "/file.jpg"})}) as (base, _):
            items = discover(base + "/page", headers={"Authorization": "Bearer secret", "Cookie": "secret=1"})
            assert "Authorization" not in items[0].headers
            assert "Cookie" not in items[0].headers
            assert items[1].headers["Authorization"] == "Bearer secret"
            [item] = discover(base + "/redirect", headers={"Authorization": "Bearer secret", "Cookie": "secret=1"})
            assert all(not any(k.lower() in {"authorization", "cookie"} for k in headers) for _, _, headers in other_seen)
            assert not any(k.lower() in {"authorization", "cookie"} for k in item.headers)


def test_xml_entities_are_rejected():
    xml = b'<!DOCTYPE rss [<!ENTITY x "boom">]><rss><channel><item><title>&x;</title></item></channel></rss>'
    with server({"/": (200, "application/rss+xml", xml)}) as (base, _):
        with pytest.raises(OmniError, match="declarations"):
            discover(base + "/")


@pytest.mark.parametrize("url,mime,kind,ext", [
    ("https://example.org/show.php", "image/avif", "image", "avif"),
    ("https://example.org/download", "model/gltf-binary", "model", "glb"),
    ("https://example.org/font.woff2", "application/octet-stream", "font", "woff2"),
    ("https://example.org/archive.zip", "application/zip", "archive", "zip"),
    ("https://example.org/book.epub", "application/epub+zip", "document", "epub"),
    ("https://example.org/song.flac?token=x", "audio/flac", "audio", "flac"),
])
def test_media_type_registry(url, mime, kind, ext):
    assert classify(url, mime) == (kind, ext)


def test_large_direct_text_does_not_hit_document_limit():
    with server({"/download": (200, "text/plain", b"a" * (MAX_DOCUMENT_BYTES + 1))}) as (base, _):
        [item] = discover(base + "/download")
        assert item.kind == "document"


def test_gltf_and_svg_with_generic_content_type_are_direct_media():
    routes = {
        "/model.gltf": (200, "application/json", b'{"asset":{"version":"2.0"}}'),
        "/image.svg": (200, "application/xml", b'<svg/>'),
    }
    with server(routes) as (base, seen):
        assert discover(base + "/model.gltf")[0].kind == "model"
        assert discover(base + "/image.svg")[0].kind == "image"
        assert all(method == "HEAD" for method, _, _ in seen)


def test_malformed_json_feed_has_actionable_error():
    feed = json.dumps({"version": "https://jsonfeed.org/version/1.1", "items": [{"attachments": 12}]}).encode()
    with server({"/": (200, "application/feed+json", feed)}) as (base, _):
        with pytest.raises(OmniError, match="attachments"):
            discover(base + "/")


def test_xhtml_xml_declaration_is_still_html():
    html = b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><img src="image.jpg"/></body></html>'
    with server({"/": (200, "application/xhtml+xml", html)}) as (base, _):
        [item] = discover(base + "/")
        assert item.kind == "image"


def test_custom_secret_headers_do_not_cross_origins():
    with server({"/file": (200, "image/png", b"x")}) as (other, seen):
        with server({"/redirect": (302, "", b"", {"Location": other + "/file"})}) as (base, _):
            [item] = discover(base + "/redirect", headers={"X-Api-Key": "secret", "Accept-Language": "en"})
            assert item.source_url == base + "/redirect"
            assert "X-Api-Key" not in item.headers
            assert all("X-Api-Key" not in headers for _, _, headers in seen)
            assert item.headers["Accept-Language"] == "en"


def test_feed_inline_filename_does_not_prevent_parsing():
    feed = b'<rss><channel><item><enclosure url="episode.mp3" type="audio/mpeg"/></item></channel></rss>'
    with server({"/": (200, "application/rss+xml", feed, {"Content-Disposition": 'inline; filename="feed.rss"'})}) as (base, _):
        [item] = discover(base + "/")
        assert item.kind == "audio"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-16-le", "utf-16-be", "utf-32", "utf-32-le", "utf-32-be"])
def test_xml_dtd_rejection_is_independent_of_document_encoding(encoding):
    # UTF-32 may be rejected by Expat itself; either result must be an actionable
    # error before entity-containing media records are produced.
    xml = '<!DOCTYPE rss [<!ENTITY injected "injected">]><rss><channel><item><title>&injected;</title><enclosure url="episode.mp3"/></item></channel></rss>'.encode(encoding)
    with server({"/": (200, "application/rss+xml", xml)}) as (base, _):
        with pytest.raises(OmniError, match="XML"):
            discover(base + "/")


def test_ordinary_utf16_feed_remains_supported():
    xml = '<?xml version="1.0" encoding="UTF-16"?><rss><channel><item><title>Episode</title><enclosure url="episode.mp3"/></item></channel></rss>'.encode("utf-16")
    with server({"/": (200, "application/rss+xml", xml)}) as (base, _):
        [item] = discover(base + "/")
        assert item.kind == "audio"
        assert item.title == "Episode"


def test_discovery_encodes_unicode_request_paths():
    with server({"/caf%C3%A9.jpg": (200, "image/jpeg", b"image")}) as (base, seen):
        [item] = discover(base + "/café.jpg")
        assert item.kind == "image"
        assert item.source_url == base + "/café.jpg"
        assert seen[0][1] == "/caf%C3%A9.jpg"
