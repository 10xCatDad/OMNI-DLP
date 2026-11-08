"""Synthetic player responses plus a real HTTP transfer through the native path."""
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.extractors import youtube
from omni_dlp.models import OmniError
from omni_dlp.transfer import TransferOptions, download

ID = "AbCdEfGhI_1"
URL = "https://www.youtube.com/watch?v=" + ID


def response():
    return {
        "playabilityStatus": {"status": "OK"},
        "videoDetails": {"videoId": ID, "title": 'Fixture }; braces " and 100% audio',
                         "lengthSeconds": "40", "author": "Fixture Author", "channelId": "fixture-channel",
                         "shortDescription": "A fixture", "viewCount": "123", "isLiveContent": False,
                         "thumbnail": {"thumbnails": [{"url": "https://img.example.invalid/full.jpg", "width": 1920, "height": 1080}]}},
        "microformat": {"playerMicroformatRenderer": {"uploadDate": "2026-01-02"}},
        "streamingData": {
            "formats": [{"itag": 18, "url": "https://media.example.invalid/combined?x=1",
                         "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
                         "width": 640, "height": 360, "fps": 30, "bitrate": 500000, "contentLength": "512"}],
            "adaptiveFormats": [
                {"itag": 137, "url": "https://media.example.invalid/video", "mimeType": 'video/mp4; codecs="avc1.640028"',
                 "width": 1920, "height": 1080, "fps": 60, "bitrate": 2000000},
                {"itag": 140, "url": "https://media.example.invalid/audio", "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
                 "bitrate": 128000, "audioChannels": 2, "audioSampleRate": "44100",
                 "audioTrack": {"id": "en.4", "displayName": "English", "audioIsDefault": True}},
            ],
        },
        "captions": {"playerCaptionsTracklistRenderer": {"captionTracks": [
            {"baseUrl": "https://www.youtube.com/api/timedtext?v=fixture&fmt=srv3", "languageCode": "en", "name": {"simpleText": "English"}},
            {"baseUrl": "https://www.youtube.com/api/timedtext?v=fixture&kind=asr", "languageCode": "en", "kind": "asr", "name": {"runs": [{"text": "Auto English"}]}},
        ]}},
    }


def page(value=None, initial=None, assignment="var ytInitialPlayerResponse = "):
    result = "<html><script>" + assignment + json.dumps(response() if value is None else value) + ";</script>"
    if initial is not None:
        result += "<script>var ytInitialData=" + json.dumps(initial) + ";</script>"
    return result + "</html>"


class FixtureContext(ExtractorContext):
    def __init__(self, html, source_url=URL, **kwargs):
        super().__init__(source_url=source_url, **kwargs)
        self.html = html
        self.requests = []

    def get_text(self, url):
        self.requests.append(url)
        return self.html


@pytest.mark.parametrize("url", [URL, "https://youtube.com/watch?v=" + ID + "&list=fixture", "https://m.youtube.com/watch?v=" + ID,
    "https://music.youtube.com/watch?v=" + ID, "https://youtu.be/" + ID + "?t=2", "http://www.youtu.be/" + ID,
    "https://www.youtube.com/embed/" + ID, "https://www.youtube-nocookie.com/embed/" + ID,
    "https://youtube.com/shorts/" + ID, "https://youtube.com/live/" + ID + "/", "https://youtube.com/v/" + ID])
def test_recognizes_single_video_urls(url):
    assert youtube.supports(url)
    context = FixtureContext(page(), source_url=url)
    [item] = youtube.extract(url, context)
    assert item.id == ID and item.source_url == url
    assert context.requests == [URL + "&hl=en"]


@pytest.mark.parametrize("url", ["https://youtube.com.evil.invalid/watch?v=" + ID, "https://evil.invalid/youtube.com/watch?v=" + ID,
    "https://user:password@youtube.com/watch?v=" + ID, "file:///watch?v=" + ID, "https://youtube.com:8080/watch?v=" + ID,
    "https://youtu.be/short", "https://youtu.be/" + ID + "/extra", "https://youtube.com/watch?v=" + ID + "&v=" + ID,
    "https://youtube.com/playlist?list=fixture", "https://youtube.com/@fixture", "https://youtube.com/watch?v=" + ID + "extra"])
def test_rejects_unsupported_or_spoofed_urls(url):
    assert not youtube.supports(url)


def test_extracts_independent_format_records_and_metadata():
    [item] = youtube.extract(URL, FixtureContext(page(), headers={"Authorization": "private", "X-Api-Key": "private"}))
    assert item.metadata["extractor"] == "youtube"
    assert item.metadata["archive_id"] == "youtube:" + ID
    assert item.metadata["duration"] == 40
    assert item.metadata["view_count"] == 123
    assert item.metadata["upload_date"] == "20260102"
    formats = {f.format_id: f for f in item.formats}
    assert set(formats) == {"18", "137", "140-en.4"}
    assert formats["18"].vcodec == "avc1.42001E" and formats["18"].acodec == "mp4a.40.2"
    assert formats["137"].height == 1080 and formats["137"].fps == 60 and formats["137"].acodec == "none"
    assert formats["140-en.4"].vcodec == "none" and formats["140-en.4"].ext == "m4a"
    assert formats["140-en.4"].abr == 128 and formats["140-en.4"].language == "en"
    assert formats["18"].filesize == 512
    assert all("Authorization" not in f.headers and "X-Api-Key" not in f.headers for f in item.formats)


@pytest.mark.parametrize("assignment", ['window["ytInitialPlayerResponse"] = ', "self['ytInitialPlayerResponse']=", "ytInitialPlayerResponse="])
def test_assignment_variants_and_embedded_json_string_characters(assignment):
    [item] = youtube.extract(URL, FixtureContext(page(assignment=assignment)))
    assert '}; braces " and 100%' in item.title


def test_json_script_tag_json_parse_and_legacy_player_config():
    serialized = json.dumps(response())
    for html in [f'<script id="ytInitialPlayerResponse" type="application/json">{serialized}</script>',
                 '<script>ytInitialPlayerResponse=JSON.parse(' + json.dumps(serialized) + ');</script>',
                 '<script>ytplayer.config=' + json.dumps({"args": {"player_response": serialized}}) + ';</script>']:
        assert youtube.extract(URL, FixtureContext(html))[0].id == ID


def test_wrong_video_response_never_downloads_another_video():
    wrong = response()
    wrong["videoDetails"]["videoId"] = "ZzZzZzZzZ_9"
    with pytest.raises(OmniError, match="identity"):
        youtube.extract(URL, FixtureContext(page(wrong)))
    html = page(wrong) + page(response())
    assert youtube.extract(URL, FixtureContext(html))[0].id == ID


@pytest.mark.parametrize("status", ["LOGIN_REQUIRED", "UNPLAYABLE", "AGE_CHECK_REQUIRED", "LIVE_STREAM_OFFLINE", "ERROR"])
def test_access_restrictions_produce_actionable_sanitized_errors(status):
    value = {"playabilityStatus": {"status": status, "reason": "secret query https://example.invalid/?token=secret"}}
    with pytest.raises(OmniError, match=status) as error:
        youtube.extract(URL, FixtureContext(page(value)))
    assert "secret" not in str(error.value)


def test_malformed_or_missing_player_is_explicit():
    for html in ["<html>Consent page</html>", '<script>ytInitialPlayerResponse={broken};</script>',
                 '<script>ytInitialPlayerResponse=[];</script>', '<script>ytInitialPlayerResponse=alert(1);</script>']:
        with pytest.raises(OmniError, match="no supported player response"):
            youtube.extract(URL, FixtureContext(html))


def test_plain_signature_cipher_is_encoded_without_execution():
    value = response()
    candidate = value["streamingData"]["formats"][0]
    candidate.pop("url")
    candidate["signatureCipher"] = urlencode({"url": "https://media.example.invalid/play?source=fixture", "sig": "already+signed/clear", "sp": "sig"})
    [item] = youtube.extract(URL, FixtureContext(page(value)))
    fmt = next(f for f in item.formats if f.format_id == "18")
    assert parse_qs(urlsplit(fmt.url).query) == {"source": ["fixture"], "sig": ["already+signed/clear"]}


def test_unsupported_candidates_are_omitted_with_precise_reasons():
    value = response()
    base = value["streamingData"]["formats"][0]
    value["streamingData"]["formats"].extend([
        {**base, "itag": 2, "url": None, "signatureCipher": urlencode({"url": base["url"], "s": "encrypted"})},
        {**base, "itag": 3, "url": base["url"] + "&n=unsolved"},
        {**base, "itag": 4, "drmFamilies": ["WIDEVINE"]},
        {**base, "itag": 5, "url": "file:///private-file"},
        {**base, "itag": 6, "url": "https://user:private@media.example.invalid/video"},
    ])
    [item] = youtube.extract(URL, FixtureContext(page(value)))
    assert len(item.formats) == 3
    assert item.metadata["omitted_formats"] == {"encrypted_signature": 1, "throttling_challenge": 1, "drm": 1, "missing_url": 2}


def test_all_challenged_formats_do_not_fall_back_to_downloading_html():
    value = response()
    value["streamingData"] = {"formats": [{"itag": 18, "signatureCipher": urlencode({"url": "https://media.example.invalid/play", "s": "secret-signature"})}],
                              "serverAbrStreamingUrl": "https://media.example.invalid/sabr"}
    with pytest.raises(OmniError, match="encrypted_signature=1.*sabr_endpoint=1") as error:
        youtube.extract(URL, FixtureContext(page(value)))
    assert "secret-signature" not in str(error.value)


def test_metadata_only_preserves_public_metadata_when_formats_need_challenges():
    value = response()
    value["streamingData"] = {"formats": [{"itag": 18, "signatureCipher": urlencode({
        "url": "https://media.example.invalid/play", "s": "fixture-signature"})}]}
    [item] = youtube.extract(URL, FixtureContext(page(value), metadata_only=True))
    assert item.formats == [] and item.url == URL
    assert item.metadata["formats_required"] is True
    assert item.metadata["subtitles"]["en"]
    assert item.metadata["thumbnails"]
    assert item.metadata["omitted_formats"] == {"encrypted_signature": 1}
    with pytest.raises(OmniError, match="no independently usable formats"):
        youtube.extract(URL, FixtureContext(page(value)))


def test_metadata_only_does_not_override_access_restrictions_or_missing_content():
    value = {"playabilityStatus": {"status": "LOGIN_REQUIRED"}}
    with pytest.raises(OmniError, match="LOGIN_REQUIRED"):
        youtube.extract(URL, FixtureContext(page(value), metadata_only=True))
    value = {"playabilityStatus": {"status": "OK"}, "videoDetails": {"videoId": ID}}
    with pytest.raises(OmniError, match="no independently usable formats"):
        youtube.extract(URL, FixtureContext(page(value), metadata_only=True))


def test_clear_manifests_are_formats_and_manifest_challenges_are_omitted():
    value = response()
    value["streamingData"] = {"hlsManifestUrl": "https://media.example.invalid/hls/stream.m3u8", "dashManifestUrl": "https://media.example.invalid/dash/stream.mpd"}
    [item] = youtube.extract(URL, FixtureContext(page(value)))
    assert [(f.format_id, f.protocol) for f in item.formats] == [("hls", "hls"), ("dash", "dash")]
    value["streamingData"]["hlsManifestUrl"] = "https://media.example.invalid/n/unsolved/stream.m3u8"
    [item] = youtube.extract(URL, FixtureContext(page(value)))
    assert [f.format_id for f in item.formats] == ["dash"]
    assert item.metadata["omitted_formats"]["throttling_challenge"] == 1


def test_audio_only_selects_audio_representations_without_losing_metadata():
    [item] = youtube.extract(URL, FixtureContext(page(), media_types={"audio"}))
    assert item.kind == "audio"
    assert len(item.formats) == 1 and item.formats[0].vcodec == "none"
    assert item.metadata["subtitles"]["en"][0]["ext"] == "vtt"
    assert parse_qs(urlsplit(item.metadata["subtitles"]["en"][0]["url"]).query)["fmt"] == ["vtt"]
    assert item.metadata["automatic_captions"]["en"][0]["name"] == "Auto English"


def test_image_and_subtitle_asset_downloads_are_explicit_and_bounded():
    value = response()
    value.pop("streamingData")
    items = youtube.extract(URL, FixtureContext(page(value), media_types={"image", "subtitle"}))
    assert [item.kind for item in items] == ["image", "subtitle", "subtitle"]
    assert len({item.metadata["archive_id"] for item in items}) == 3
    assert all(not item.formats for item in items)
    assert len(youtube.extract(URL, FixtureContext(page(value), media_types={"image", "subtitle"}, max_items=2))) == 2
    assert youtube.extract(URL, FixtureContext(page(value), media_types={"document"})) == []


def test_chapters_have_valid_sorted_ranges():
    initial = {"playerOverlays": {"playerOverlayRenderer": {"chapters": [
        {"chapterRenderer": {"title": {"simpleText": "Second"}, "timeRangeStartMillis": 20000}},
        {"chapterRenderer": {"title": {"runs": [{"text": "First"}]}, "timeRangeStartMillis": 0}},
        {"chapterRenderer": {"title": {"simpleText": "Outside"}, "timeRangeStartMillis": 50000}},
        {"chapterRenderer": {"title": {"simpleText": "Invalid"}, "timeRangeStartMillis": -1}},
    ]}}}
    [item] = youtube.extract(URL, FixtureContext(page(initial=initial)))
    assert item.metadata["chapters"] == [{"start_time": 0, "end_time": 20, "title": "First"}, {"start_time": 20, "end_time": 40, "title": "Second"}]


def test_malformed_format_fields_are_bounded_and_do_not_crash():
    value = response()
    raw = value["streamingData"]["formats"][0]
    raw.update(width=True, height="nan", contentLength="Infinity", fps=-5, bitrate={}, audioTrack=[])
    value["streamingData"]["adaptiveFormats"] = [None, [], {**raw, "itag": None}, {**raw, "itag": 24, "mimeType": []}]
    [item] = youtube.extract(URL, FixtureContext(page(value)))
    assert len(item.formats) == 1
    assert item.formats[0].width is None and item.formats[0].height is None
    assert item.formats[0].filesize is None and item.formats[0].fps is None


@contextmanager
def http_fixture():
    payload = b"native media fixture\0" * 100
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = routes.get(self.path, b"missing")
            self.send_response(200 if self.path in routes else 404)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    routes["/media.mp4"] = payload
    value = response()
    value["streamingData"]["adaptiveFormats"] = []
    value["streamingData"]["formats"][0]["url"] = base + "/media.mp4"
    routes["/watch"] = page(value).encode()
    try:
        yield base, payload
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_actual_http_response_and_byte_transfer_without_upstream(tmp_path):
    with http_fixture() as (base, expected):
        class LocalContext(ExtractorContext):
            def get_text(self, url):
                assert url == URL + "&hl=en"
                return super().get_text(base + "/watch")

        [media] = youtube.extract(URL, LocalContext(source_url=URL))
        chosen = media.formats[0]
        result = download(replace(media, url=chosen.url, ext=chosen.ext), TransferOptions(directory=str(tmp_path)))
        assert result.size == len(expected)
        assert open(result.path, "rb").read() == expected
        assert result.sha256
