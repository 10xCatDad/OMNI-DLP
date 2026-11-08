from __future__ import annotations

import json

import pytest

from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.extractors.vimeo import extract, supports
from omni_dlp.models import OmniError


class Context(ExtractorContext):
    def __init__(self, url, configuration, **kwargs):
        super().__init__(url, **kwargs)
        self.configuration = configuration
        self.requests = []
        self.text = ""
        self.responses = {}

    def get_json(self, url):
        self.requests.append(url)
        value = self.responses.get(url, self.configuration)
        if isinstance(value, Exception):
            raise value
        return value

    def get_text(self, url):
        self.requests.append(url)
        return self.text


def config():
    return {"video": {"id": 123, "title": "Fixture video", "duration": 12.5,
                      "owner": {"name": "Fixture", "url": "https://vimeo.com/fixture"},
                      "thumbs": {"640": "https://images.example/cover.jpg"}},
            "request": {"files": {"progressive": [
                {"url": "https://media.example/360.mp4", "mime": "video/mp4", "quality": "360p", "width": 640, "height": 360, "fps": 30},
                {"url": "https://media.example/720.mp4", "mime": "video/mp4", "quality": "720p", "width": 1280, "height": 720}],
                "hls": {"default_cdn": "edge", "cdns": {"edge": {"url": "https://media.example/master.m3u8"}}},
                "dash": {"cdns": {"edge": {"url": "https://media.example/master.mpd"}}},
                "hls_cenc": {"cdns": {"protected": {"url": "https://media.example/protected.m3u8"}}}},
                "text_tracks": [{"url": "/texttrack/1.vtt", "lang": "en", "label": "English"}]},
            "embed": {"chapters": [{"timecode": 5, "title": "Second"}, {"timecode": 0, "title": "First"}]}}


@pytest.mark.parametrize("url,expected", [
    ("https://vimeo.com/123", True),
    ("https://www.vimeo.com/123/abc123", True),
    ("https://player.vimeo.com/video/123?h=abc123", True),
    ("https://vimeo.com/channels/staffpicks/123", True),
    ("https://vimeo.com/groups/shorts/videos/123", True),
    ("https://vimeo.com/showcase/42/video/123", True),
    ("https://vimeo.com/username", False),
    ("https://vimeo.com/123abc", False),
    ("https://vimeo.com.evil.test/123", False),
    ("https://user@vimeo.com/123", False),
    ("file:///123", False),
    ("https://player.vimeo.com/video/123?h=%0aBAD", False),
])
def test_url_scope(url, expected):
    assert supports(url) is expected


def test_progressive_and_clear_manifests_metadata_and_credential_scoping():
    url = "https://vimeo.com/123"
    context = Context(url, config(), headers={"Authorization": "private", "X-Api-Key": "private"})
    item = extract(url, context)[0]
    assert item.id == "123" and item.title == "Fixture video" and item.source_url == url
    assert [fmt.format_id for fmt in item.formats] == ["http-360p", "http-720p", "hls-edge", "dash-edge"]
    assert item.formats[1].height == 720 and item.formats[0].fps == 30
    assert item.formats[2].protocol == "hls" and item.formats[2].metadata["manifest"]
    assert "Authorization" not in item.headers and "X-Api-Key" not in item.headers
    assert item.metadata["subtitles"]["en"][0]["url"] == "https://player.vimeo.com/texttrack/1.vtt"
    assert [x["start_time"] for x in item.metadata["chapters"]] == [0, 5]
    assert item.metadata["archive_id"] == "vimeo:123"
    assert context.requests == ["https://player.vimeo.com/video/123/config"]


@pytest.mark.parametrize("url", ["https://vimeo.com/123/abc123", "https://player.vimeo.com/video/123?h=abc123"])
def test_unlisted_hash_preserved(url):
    context = Context(url, config())
    extract(url, context)
    assert context.requests == ["https://player.vimeo.com/video/123/config?h=abc123"]


def test_embedded_player_fallback_parses_nested_json_with_braces_in_strings():
    url = "https://vimeo.com/123"
    context = Context(url, OmniError("HTTP 403"))
    data = config()
    data["video"]["title"] = 'A } and \\" string'
    context.text = "const config = " + json.dumps(data) + "; otherJavascript();"
    item = extract(url, context)[0]
    assert item.title == data["video"]["title"]
    assert context.requests[-1] == "https://player.vimeo.com/video/123"


def test_json_dash_descriptor_resolves_to_mpd_without_dropping_query():
    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"]["dash"]["cdns"]["edge"]["url"] = "https://media.example/descriptor?json=1"
    context = Context(url, data)
    context.responses["https://media.example/descriptor?json=1"] = {"url": "https://media.example/master.json?token=fixture"}
    item = extract(url, context)[0]
    assert item.formats[-1].url == "https://media.example/master.mpd?token=fixture"


def test_malformed_optional_descriptor_does_not_discard_working_progressive():
    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"]["dash"]["cdns"]["edge"]["url"] = "https://media.example/descriptor?json=1"
    context = Context(url, data)
    context.responses["https://media.example/descriptor?json=1"] = OmniError("Failed")
    item = extract(url, context)[0]
    assert len(item.formats) == 3
    assert item.metadata["diagnostics"] == ["Could not resolve dash descriptor"]


def test_images_and_subtitles_are_independent_assets_when_explicitly_requested():
    url = "https://vimeo.com/123"
    items = extract(url, Context(url, config(), media_types={"image", "subtitle"}))
    assert [item.kind for item in items] == ["image", "subtitle"]
    assert items[1].ext == "vtt" and items[1].metadata["language"] == "en"
    assert all(not item.formats for item in items)
    assert len(extract(url, Context(url, config(), media_types={"image", "subtitle"}, max_items=1))) == 1


def test_file_urls_and_protected_formats_are_not_advertised():
    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"] = {"progressive": [
        {"url": "file:///etc/passwd"}, {"url": "https://u:p@host/a"},
        {"url": "https://media.example/drm.mp4", "drm": True}],
        "hls_cenc": {"cdns": {"edge": {"url": "https://media.example/cenc.m3u8"}}}}
    with pytest.raises(OmniError, match="no accessible clear"):
        extract(url, Context(url, data))


@pytest.mark.parametrize("data,match", [(None, "invalid player"), ({"view": 4}, "password"),
    ({"message": "Forbidden secret"}, "access-restricted"), ({"video": {"id": 456}}, "different video")])
@pytest.mark.parametrize("metadata_only", [False, True])
def test_restricted_invalid_or_mismatched_config_is_explicit(data, match, metadata_only):
    url = "https://vimeo.com/123"
    with pytest.raises(OmniError, match=match) as error:
        extract(url, Context(url, data, metadata_only=metadata_only))
    assert "secret" not in str(error.value)


def test_invalid_embedded_json_is_actionable():
    url = "https://vimeo.com/123"
    context = Context(url, OmniError("blocked"))
    context.text = "playerConfig = {malformed};"
    with pytest.raises(OmniError, match="public player configuration"):
        extract(url, context)


def test_empty_irrelevant_filter_and_malformed_optional_fields():
    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"]["progressive"][0].update(width="NaN", fps=True)
    item = extract(url, Context(url, data))[0]
    assert item.formats[0].width is None and item.formats[0].fps is None
    assert extract(url, Context(url, data, media_types={"document"})) == []


def test_metadata_only_preserves_assets_without_supported_video():
    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"] = {"hls_cenc": {"cdns": {"edge": {"url": "https://media.example/protected.m3u8"}}}}
    data["video"]["description"] = "Public description"
    item = extract(url, Context(url, data, metadata_only=True))[0]
    assert item.id == "123" and item.url == item.source_url == url
    assert not item.formats and not item.requested_formats
    assert item.metadata["formats_required"] and item.metadata["metadata_only"]
    assert item.metadata["description"] == "Public description"
    assert item.metadata["thumbnails"] and item.metadata["subtitles"]["en"]
    with pytest.raises(OmniError, match="no accessible clear"):
        extract(url, Context(url, data))


@pytest.mark.parametrize("metadata_only,media_types", [(True, None), (True, {"video"}),
                                                       (False, {"image", "subtitle"})])
def test_metadata_or_asset_only_requests_do_not_resolve_dash_indirection(metadata_only, media_types):
    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"] = {"dash": {"cdns": {"edge": {"url": "https://media.example/descriptor?json=1"}}}}
    context = Context(url, data, metadata_only=metadata_only, media_types=media_types)
    context.responses["https://media.example/descriptor?json=1"] = AssertionError("Unexpected video request")
    items = extract(url, context)
    assert items and all(not item.formats for item in items)
    assert context.requests == ["https://player.vimeo.com/video/123/config"]


@pytest.mark.parametrize("data", [{}, {"video": {}}, {"video": {"id": 123}}, {"video": {"title": " "}}])
def test_metadata_only_does_not_make_empty_or_invalid_config_a_success(data):
    url = "https://vimeo.com/123"
    with pytest.raises(OmniError, match="no accessible clear"):
        extract(url, Context(url, data, metadata_only=True))


def test_metadata_only_video_does_not_download_page_bytes(tmp_path, monkeypatch):
    from omni_dlp.engine import Downloader, Options
    from omni_dlp.transfer import TransferOptions

    url = "https://vimeo.com/123"
    data = config()
    data["request"]["files"] = {}
    item = extract(url, Context(url, data, metadata_only=True))[0]
    downloader = Downloader(Options(transfer=TransferOptions(directory=str(tmp_path))))

    def unexpected_transfer(*args, **kwargs):
        pytest.fail("Metadata-only video must never be transferred as page bytes")

    monkeypatch.setattr(downloader, "fetch_one", unexpected_transfer)
    with pytest.raises(OmniError, match="No playable format"):
        downloader.fetch([item])
    assert not list(tmp_path.iterdir())


def test_http_metadata_only_extracts_and_saves_assets_without_video(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading

    from omni_dlp.assets import AssetOptions, download_assets, plan_assets
    from omni_dlp.transfer import TransferOptions

    payloads = {"/caption.vtt": b"WEBVTT\n\n00:00.000 --> 00:01.000\nCaption\n",
                "/cover.jpg": b"\xff\xd8\xfffixture image\xff\xd9"}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            if self.path == "/config":
                base = f"http://127.0.0.1:{self.server.server_port}"
                data = config()
                data["request"]["files"] = {}
                data["video"].update(description="Fixture description", thumbs={"640": base + "/cover.jpg"})
                data["request"]["text_tracks"] = [{"url": base + "/caption.vtt", "lang": "en"}]
                body = json.dumps(data).encode()
            elif self.path in payloads:
                body = payloads[self.path]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "https://vimeo.com/123"

    class RoutedContext(ExtractorContext):
        def get_json(self, source):
            assert source == "https://player.vimeo.com/video/123/config"
            return super().get_json(f"http://127.0.0.1:{server.server_port}/config")

    try:
        item = extract(url, RoutedContext(url, metadata_only=True))[0]
        main = tmp_path / "fixture.bin"
        plans = plan_assets(item, main, AssetOptions(write_subs=True, write_thumbnail=True, write_description=True))
        results = download_assets(plans, TransferOptions(directory=str(tmp_path)))
        assert len(results) == 3 and not main.exists()
        assert (tmp_path / "fixture.en.vtt").read_bytes() == payloads["/caption.vtt"]
        assert (tmp_path / "fixture.jpg").read_bytes() == payloads["/cover.jpg"]
        assert (tmp_path / "fixture.description").read_text() == "Fixture description"
        assert requests == ["/config", "/caption.vtt", "/cover.jpg"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_http_fixture_player_and_progressive_download(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import threading
    from omni_dlp.transfer import TransferOptions, download
    payload = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/config":
                data = config()
                data["request"]["files"] = {"progressive": [{"quality": "360p", "url": f"http://127.0.0.1:{self.server.server_port}/video.mp4"}]}
                body = json.dumps(data).encode()
            else:
                body = payload
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "https://vimeo.com/123"

    class RoutedContext(ExtractorContext):
        def get_json(self, source):
            return super().get_json(f"http://127.0.0.1:{server.server_port}/config")

    try:
        item = extract(url, RoutedContext(url))[0]
        result = download(item, TransferOptions(directory=str(tmp_path)))
        assert Path(result.path).read_bytes() == payload
        assert result.size == len(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
