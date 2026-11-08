from __future__ import annotations

from html import escape
import json

import pytest

from omni_dlp.extractors.bandcamp import extract, supports
from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.models import OmniError


class Context(ExtractorContext):
    def __init__(self, url, text, **kwargs):
        super().__init__(url, **kwargs)
        self.text = text
        self.requests = []

    def get_text(self, url):
        self.requests.append(url)
        return self.text


def page(tracks, **extra):
    data = {"artist": "Fixture Artist", "current": {"id": 42, "title": "Fixture Album"}, "trackinfo": tracks, **extra}
    return '<meta property="og:image" content="https://images.example/cover.jpg"><script data-tralbum="' + escape(json.dumps(data), quote=True) + '"></script>'


def track(identifier=1, **extra):
    return {"track_id": identifier, "title": f"Track {identifier}", "duration": 12.5, "track_num": identifier,
            "title_link": f"/track/song-{identifier}", "file": {"mp3-128": f"https://audio.example/{identifier}.mp3"}, **extra}


@pytest.mark.parametrize("url,expected", [
    ("https://artist.bandcamp.com/track/song", True),
    ("https://artist.bandcamp.com/album/album/?x=1", True),
    ("https://bandcamp.com/album/album", True),
    ("https://artist.bandcamp.com/music", False),
    ("https://artist.bandcamp.com.evil.test/track/song", False),
    ("https://bandcamp.com@evil.test/track/song", False),
    ("file:///track/song", False),
    ("https://artist.bandcamp.com/track/song/more", False),
])
def test_url_scope(url, expected):
    assert supports(url) is expected


def test_album_preserves_tracks_alternatives_metadata_and_scoped_headers():
    url = "https://artist.bandcamp.com/album/album"
    first = track(file={"mp3-128": "//audio.example/one.mp3", "flac": "https://audio.example/one.flac"})
    context = Context(url, page([first, track(2)]), headers={"Authorization": "private", "X-Api-Key": "private"})
    items = extract(url, context)
    assert len(items) == 2 and items[0].id == "1" and items[1].id == "2"
    assert items[0].kind == "audio" and items[0].source_url == url
    assert [fmt.format_id for fmt in items[0].formats] == ["mp3-128", "flac"]
    assert items[0].formats[0].abr == 128 and items[0].formats[0].vcodec == "none"
    assert items[0].formats[1].abr is None
    assert items[0].metadata["artist"] == "Fixture Artist"
    assert items[1].metadata["playlist_index"] == 2 and items[0].metadata["playlist_count"] == 2
    assert items[0].metadata["collection_complete"]
    assert "Authorization" not in items[0].headers and "X-Api-Key" not in items[0].headers
    assert items[0].metadata["thumbnails"][0]["url"] == "https://images.example/cover.jpg"


def test_single_track_matches_current_among_multiple_tracks():
    url = "https://artist.bandcamp.com/track/song-2"
    items = extract(url, Context(url, page([track(1), track(2)], current={"id": 2, "title": "Song"})))
    assert len(items) == 1 and items[0].id == "2"
    assert "playlist_id" not in items[0].metadata


def test_max_items_and_unavailable_tracks_are_explicit():
    url = "https://artist.bandcamp.com/album/album"
    items = extract(url, Context(url, page([track(file=None), track(2), track(3)]), max_items=1))
    assert len(items) == 1 and items[0].id == "2"
    assert items[0].metadata["unavailable_track_ids"] == ["1"]
    assert items[0].metadata["limited"] and not items[0].metadata["collection_complete"]


def test_explicit_image_filter_returns_cover_without_audio():
    url = "https://artist.bandcamp.com/album/album"
    items = extract(url, Context(url, page([track()]), media_types={"image"}))
    assert len(items) == 1 and items[0].kind == "image" and items[0].ext == "jpg"


def test_unrelated_media_filter_does_not_report_a_failure():
    url = "https://artist.bandcamp.com/album/album"
    assert extract(url, Context(url, page([track()]), media_types={"document"})) == []


def test_invalid_media_urls_are_rejected_and_duplicate_formats_removed():
    url = "https://artist.bandcamp.com/track/song"
    files = {"unsafe": "file:///etc/passwd", "js": "javascript:alert(1)", "credential": "https://u:p@cdn.test/a",
             "mp3-128": "https://audio.example/a.mp3", "duplicate": "https://audio.example/a.mp3"}
    items = extract(url, Context(url, page([track(file=files)])))
    assert [fmt.format_id for fmt in items[0].formats] == ["mp3-128"]


@pytest.mark.parametrize("text,match", [
    ('<script data-tralbum="{bad}"></script>', "invalid embedded"),
    ('<script data-tralbum="[]"></script>', "invalid embedded"),
    ('<title>Client Challenge</title>', "browser challenge"),
    ('<html>ordinary page</html>', "unavailable"),
])
def test_invalid_player_pages_are_actionable(text, match):
    url = "https://artist.bandcamp.com/track/song"
    with pytest.raises(OmniError, match=match):
        extract(url, Context(url, text))


def test_no_accessible_audio_does_not_become_a_successful_empty_album():
    url = "https://artist.bandcamp.com/album/album"
    with pytest.raises(OmniError, match="no publicly playable"):
        extract(url, Context(url, page([track(file=None)])))


def test_track_identity_mismatch_and_nonfinite_metadata():
    url = "https://artist.bandcamp.com/track/missing"
    with pytest.raises(OmniError, match="matching track"):
        extract(url, Context(url, page([track(1), track(2)])))
    item = extract(url, Context(url, page([track(duration="NaN", track_num=True)])))[0]
    assert item.metadata["duration"] is None and item.metadata["track_number"] == 1


def test_http_fixture_extracts_then_downloads_actual_stream_bytes(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import threading
    from omni_dlp.transfer import TransferOptions, download
    payload = b"ID3" + bytes(range(256)) * 8

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/album/fixture":
                body = page([track(file={"mp3-128": f"http://127.0.0.1:{self.server.server_port}/stream.mp3"})]).encode()
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
    url = "https://fixture.bandcamp.com/album/fixture"

    class RoutedContext(ExtractorContext):
        def get_text(self, source):
            return super().get_text(f"http://127.0.0.1:{server.server_port}/album/fixture")

    try:
        item = extract(url, RoutedContext(url))[0]
        result = download(item, TransferOptions(directory=str(tmp_path)))
        assert Path(result.path).read_bytes() == payload
        assert result.size == len(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
