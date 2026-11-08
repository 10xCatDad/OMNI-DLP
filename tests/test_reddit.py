from copy import deepcopy
import json
from urllib.parse import urlsplit

import pytest

from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.extractors.reddit import extract, supports
from omni_dlp.models import OmniError


class FixtureContext(ExtractorContext):
    def __init__(self, post=None, url="https://www.reddit.com/comments/abc123", **kwargs):
        super().__init__(source_url=url, **kwargs)
        self.payload = listing(post or {"id": "abc123", "title": "Fixture", "url": "https://i.redd.it/photo.jpg"})
        self.responses = {}
        self.calls = []

    def get_document(self, url, *, headers=None):
        self.calls.append(url)
        if url in self.responses:
            response = self.responses[url]
            if isinstance(response, Exception):
                raise response
            return response
        return json.dumps(deepcopy(self.payload)).encode(), url, self.scoped_headers(url)


def listing(post):
    return [{"kind": "Listing", "data": {"children": [{"kind": "t3", "data": post}]}},
            {"kind": "Listing", "data": {"children": []}}]


def video(**changes):
    return {"fallback_url": "https://v.redd.it/video123/DASH_720.mp4?source=fallback", "width": 1280,
            "height": 720, "duration": 12.5, "bitrate_kbps": 1600,
            "hls_url": "https://v.redd.it/video123/HLSPlaylist.m3u8?a=1&amp;b=2",
            "dash_url": "https://v.redd.it/video123/DASHPlaylist.mpd", **changes}


def gallery():
    return {"id": "abc123", "title": "Two images", "is_gallery": True,
            "gallery_data": {"items": [{"media_id": "pic2", "caption": "Second first"}, {"media_id": "pic1"}]},
            "media_metadata": {
                "pic1": {"status": "valid", "e": "Image", "m": "image/png", "s": {"u": "https://i.redd.it/pic1.png"}},
                "pic2": {"status": "valid", "e": "Image", "m": "image/jpeg", "s": {"u": "https://i.redd.it/pic2.jpg?a=1&amp;b=2"},
                         "p": [{"u": "https://preview.redd.it/small.jpg"}]},
            }}


@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/test/comments/abc123/title/", "https://old.reddit.com/comments/abc123/.json",
    "https://reddit.com/user/example/comments/abc123/title", "https://www.reddit.com/gallery/abc123",
    "https://redd.it/abc123", "https://i.redd.it/image123.png", "https://v.redd.it/video123",
    "https://v.redd.it/video123/DASH_720.mp4", "https://www.redditmedia.com/r/test/comments/abc123/title",
])
def test_supported_url_shapes(url):
    assert supports(url)


@pytest.mark.parametrize("url", [
    "https://reddit.com.evil.test/comments/abc123", "file:///comments/abc123", "https://www.reddit.com/r/test/new",
    "https://user:password@reddit.com/comments/abc123", "https://www.reddit.com:444/comments/abc123",
    "https://i.redd.it/../../secret", "https://v.redd.it/%2e%2e/path", "https://reddit.com/comments/ab%00c",
    "https://redd.it/abc123/extra", "https://www.reddit.com/search?q=hello",
])
def test_rejects_nonmedia_or_unsafe_urls(url):
    assert not supports(url)


def test_post_video_exposes_real_alternatives_and_metadata():
    post = {"id": "abc123", "title": "A fixture clip", "author": "example", "subreddit": "test",
            "created_utc": 1700000000, "num_comments": 3, "secure_media": {"reddit_video": video()},
            "media": {"reddit_video": video()}, "preview": {"images": [{"source": {"url": "https://i.redd.it/cover.jpg"}}]}}
    context = FixtureContext(post)
    result = extract(context.source_url, context)
    assert len(result) == 1
    item = result[0]
    assert item.id == "video123" and item.kind == "video"
    assert [f.format_id for f in item.formats] == ["hls", "dash", "fallback"]
    assert item.formats[0].url.endswith("?a=1&b=2")
    assert item.formats[-1].vcodec == "h264" and item.formats[-1].acodec == "none"
    assert item.formats[-1].height == 720 and item.formats[-1].tbr == 1600
    assert item.metadata["duration"] == 12.5
    assert item.metadata["archive_id"] == "reddit:video:video123"
    assert item.metadata["playlist_count"] == 1
    assert item.metadata["thumbnails"] == [{"url": "https://i.redd.it/cover.jpg", "width": None, "height": None}]
    assert context.calls == ["https://www.reddit.com/comments/abc123.json?raw_json=1&limit=0"]


def test_gallery_originals_preserve_order_and_caption_not_previews():
    context = FixtureContext(gallery())
    items = extract(context.source_url, context)
    assert [urlsplit(m.url).path for m in items] == ["/pic2.jpg", "/pic1.png"]
    assert items[0].url.endswith("?a=1&b=2")
    assert items[0].metadata["caption"] == "Second first"
    assert [m.metadata["playlist_index"] for m in items] == [1, 2]
    assert all(m.metadata["playlist_count"] == 2 and m.metadata["collection_complete"] for m in items)
    assert len({m.id for m in items}) == 2


def test_animated_gallery_has_image_and_silent_video_assets():
    post = gallery()
    post["gallery_data"]["items"] = [{"media_id": "anim"}]
    post["media_metadata"] = {"anim": {"status": "valid", "e": "AnimatedImage", "m": "image/gif",
                                      "s": {"gif": "https://i.redd.it/animation.gif", "mp4": "https://i.redd.it/animation.mp4"}}}
    context = FixtureContext(post)
    items = extract(context.source_url, context)
    assert [m.kind for m in items] == ["image", "video"]
    assert items[1].formats[0].acodec == "none"
    assert items[0].metadata["playlist_index"] == items[1].metadata["playlist_index"] == 1
    context.media_types = {"image"}
    assert len(extract(context.source_url, context)) == 1


def test_private_missing_gallery_entry_preserves_indices_and_reports_gap():
    post = gallery()
    post["media_metadata"]["pic2"]["status"] = "failed"
    context = FixtureContext(post)
    items = extract(context.source_url, context)
    assert len(items) == 1
    assert items[0].metadata["playlist_index"] == 2
    assert items[0].metadata["playlist_count"] == 2
    assert items[0].metadata["collection_omitted"] == 1
    assert items[0].metadata["collection_complete"] is False


@pytest.mark.parametrize("status", [[], {}])
def test_malformed_gallery_status_is_omitted_safely(status):
    post = gallery()
    post["media_metadata"]["pic2"]["status"] = status
    context = FixtureContext(post)
    items = extract(context.source_url, context)
    assert len(items) == 1 and items[0].metadata["collection_omitted"] == 1


def test_max_items_marks_collection_incomplete():
    context = FixtureContext(gallery(), max_items=1)
    items = extract(context.source_url, context)
    assert len(items) == 1 and items[0].metadata["collection_truncated"] is True
    assert items[0].metadata["playlist_count"] == 2


def test_embedded_selfpost_media_includes_images_audio_and_video():
    post = {"id": "abc123", "title": "Mixed", "media_metadata": {
        "photo": {"e": "Image", "m": "image/png", "s": {"u": "https://i.redd.it/photo.png"}},
        "clip": {"e": "RedditVideo", "id": "clip123", "hlsUrl": "https://v.redd.it/clip123/HLSPlaylist.m3u8"},
        "sound": {"m": "audio/mpeg", "s": {"u": "https://example.test/song.mp3"}},
    }}
    context = FixtureContext(post)
    items = extract(context.source_url, context)
    assert [m.kind for m in items] == ["image", "video", "audio"]
    assert [m.metadata["playlist_index"] for m in items] == [1, 2, 3]


def test_crosspost_metadata_has_bounded_recursion_and_keeps_requested_title():
    original = {"id": "orig123", "author": "original", "title": "Original title", "media": {"reddit_video": video()}}
    post = {"id": "abc123", "title": "Crosspost title", "author": "poster", "crosspost_parent_list": [original]}
    context = FixtureContext(post)
    items = extract(context.source_url, context)
    assert len(items) == 1 and items[0].title == "Crosspost title"
    assert items[0].metadata["post_id"] == "abc123" and items[0].metadata["source_post_id"] == "orig123"
    assert items[0].source_url == context.source_url


def test_deep_crosspost_is_reported_without_hanging():
    post = {"id": "abc123", "title": "Deep", "url": "https://i.redd.it/root.jpg"}
    current = post
    for index in range(8):
        child = {"id": "post" + str(index), "crosspost_parent_list": []}
        current["crosspost_parent_list"] = [child]
        current = child
    current["url"] = "https://i.redd.it/deep.jpg"
    context = FixtureContext(post)
    items = extract(context.source_url, context)
    assert len(items) == 1 and items[0].url.endswith("/root.jpg")
    assert items[0].metadata["collection_complete"] is False


def test_external_direct_audio_is_a_native_asset():
    context = FixtureContext({"id": "abc123", "title": "Audio", "url_overridden_by_dest": "https://example.test/song.flac"})
    item = extract(context.source_url, context)[0]
    assert item.kind == "audio" and item.formats[0].vcodec == "none"


def test_external_page_is_not_falsely_returned_as_media():
    context = FixtureContext({"id": "abc123", "title": "Link", "url": "https://example.test/watch/123"})
    with pytest.raises(OmniError, match="external-page delegation"):
        extract(context.source_url, context)


def test_metadata_only_preserves_public_post_without_native_media():
    context = FixtureContext({"id": "abc123", "title": "Text post", "selftext": "Public description",
                              "url": "https://example.test/watch/123"})
    context.metadata_only = True
    item = extract(context.source_url, context)[0]
    assert item.metadata["description"] == "Public description"
    assert item.metadata["formats_required"] is True and item.metadata["metadata_only"] is True
    assert item.url == context.source_url and not item.formats


def test_metadata_only_does_not_bypass_unavailable_public_metadata():
    context = FixtureContext()
    context.metadata_only = True
    context.payload = {"error": 403, "reason": "private"}
    with pytest.raises(OmniError):
        extract(context.source_url, context)


def test_non_http_child_is_omitted():
    post = gallery()
    post["media_metadata"]["pic2"]["s"]["u"] = "file:///etc/passwd"
    context = FixtureContext(post)
    item = extract(context.source_url, context)[0]
    assert item.url.endswith("pic1.png") and item.metadata["collection_omitted"] == 1


@pytest.mark.parametrize("payload", [{"error": 403, "reason": "private"}, [], {"unexpected": 1}, listing({"id": "wrong"})])
def test_unavailable_or_wrong_post_fails(payload):
    context = FixtureContext()
    context.payload = payload
    with pytest.raises(OmniError):
        extract(context.source_url, context)


def test_login_redirect_is_not_treated_as_media():
    context = FixtureContext()
    endpoint = "https://www.reddit.com/comments/abc123.json?raw_json=1&limit=0"
    context.responses[endpoint] = (b"<html>login</html>", "https://www.reddit.com/login/", {})
    with pytest.raises(OmniError, match="login"):
        extract(context.source_url, context)


def test_direct_hosted_video_probes_manifest_types():
    context = FixtureContext(url="https://v.redd.it/video123")
    hls, dash = "https://v.redd.it/video123/HLSPlaylist.m3u8", "https://v.redd.it/video123/DASHPlaylist.mpd"
    context.responses = {hls: (b"#EXTM3U\n#EXT-X-ENDLIST\n", hls, {}), dash: (b'<?xml version="1.0"?><MPD/>', dash, {})}
    item = extract(context.source_url, context)[0]
    assert [f.protocol for f in item.formats] == ["hls", "dash"]
    assert item.metadata["metadata_unavailable"] is True
    assert item.metadata["archive_id"] == "reddit:video:video123"


def test_hosted_video_reports_one_failed_manifest_and_uses_other():
    context = FixtureContext(url="https://v.redd.it/video123")
    hls, dash = "https://v.redd.it/video123/HLSPlaylist.m3u8", "https://v.redd.it/video123/DASHPlaylist.mpd"
    context.responses = {hls: OmniError("Site request failed with HTTP 404"), dash: (b"<MPD/>", dash, {})}
    item = extract(context.source_url, context)[0]
    assert len(item.formats) == 1 and item.formats[0].protocol == "dash"
    assert item.metadata["format_warnings"]


def test_hosted_video_rejects_html_errors():
    context = FixtureContext(url="https://v.redd.it/video123")
    for filename in ("HLSPlaylist.m3u8", "DASHPlaylist.mpd"):
        target = "https://v.redd.it/video123/" + filename
        context.responses[target] = (b"<html>not a manifest</html>", target, {})
    with pytest.raises(OmniError, match="no accessible"):
        extract(context.source_url, context)


def test_direct_image_and_audio_representation():
    context = FixtureContext(url="https://i.redd.it/photo123.webp")
    image = extract(context.source_url, context)[0]
    assert image.kind == "image" and image.ext == "webp" and not context.calls
    context.source_url = "https://v.redd.it/video123/DASH_audio.mp4"
    audio = extract(context.source_url, context)[0]
    assert audio.kind == "audio" and audio.formats[0].vcodec == "none"


def test_headers_never_cross_from_post_to_asset_origin():
    context = FixtureContext(headers={"Authorization": "private-token", "X-Api-Key": "private-key"})
    item = extract(context.source_url, context)[0]
    assert not any(key.lower() in {"authorization", "x-api-key"} for key in item.headers)
    assert item.source_url == context.source_url


def test_subtitle_url_and_bad_numeric_metadata():
    context = FixtureContext({"id": "abc123", "media": {"reddit_video": video(
        caption_url="https://v.redd.it/video123/captions.vtt", width="NaN", height=True, duration="Infinity")}})
    item = extract(context.source_url, context)[0]
    assert item.metadata["subtitles"]["und"][0]["url"].endswith("captions.vtt")
    assert item.formats[0].width is None and item.formats[0].height is None and item.metadata["duration"] is None


def test_subtitle_language_is_preserved_when_advertised():
    context = FixtureContext({"id": "abc123", "media": {"reddit_video": video(
        caption_url="https://v.redd.it/video123/captions.vtt", caption_language="pt-BR")}})
    item = extract(context.source_url, context)[0]
    assert list(item.metadata["subtitles"]) == ["pt-BR"]


def test_direct_image_host_video_retains_actual_media_kind():
    context = FixtureContext(url="https://i.redd.it/animation.mp4", media_types={"video"})
    item = extract(context.source_url, context)[0]
    assert item.kind == "video" and item.formats[0].format_id == "original"


def test_direct_image_variants_have_distinct_stable_archive_ids():
    contexts = [FixtureContext(url="https://i.redd.it/animation" + suffix)
                for suffix in (".gif?token=one", ".gif?token=two", ".mp4")]
    items = [extract(context.source_url, context)[0] for context in contexts]
    assert items[0].metadata["archive_id"] == items[1].metadata["archive_id"]
    assert items[0].metadata["archive_id"] != items[2].metadata["archive_id"]


def test_malformed_crosspost_is_recorded_as_omitted():
    context = FixtureContext({"id": "abc123", "url": "https://i.redd.it/photo.jpg", "crosspost_parent_list": [None]})
    item = extract(context.source_url, context)[0]
    assert item.metadata["collection_omitted"] == 1 and not item.metadata["collection_complete"]


def test_gallery_original_dimensions():
    post = gallery()
    post["media_metadata"]["pic2"]["s"].update(x=1920, y=1080)
    item = extract("https://reddit.com/gallery/abc123", FixtureContext(post))[0]
    assert item.metadata["width"] == 1920 and item.metadata["height"] == 1080


def test_post_without_video_object_can_resolve_public_hosted_manifests():
    context = FixtureContext({"id": "abc123", "title": "Legacy video", "url": "https://v.redd.it/video123"})
    for filename, body in (("HLSPlaylist.m3u8", b"#EXTM3U"), ("DASHPlaylist.mpd", b"<MPD/>")):
        target = "https://v.redd.it/video123/" + filename
        context.responses[target] = (body, target, {"User-Agent": "safe-agent"})
    item = extract(context.source_url, context)[0]
    assert item.title == "Legacy video" and item.metadata["post_id"] == "abc123"
    assert all(fmt.headers == {"User-Agent": "safe-agent"} for fmt in item.formats)


def test_hosted_manifest_preserves_final_origin_headers():
    context = FixtureContext(url="https://v.redd.it/video123", headers={"Authorization": "fixture-token"})
    hls, dash = "https://v.redd.it/video123/HLSPlaylist.m3u8", "https://v.redd.it/video123/DASHPlaylist.mpd"
    context.responses = {hls: (b"#EXTM3U", "https://cdn.example.test/master.m3u8", {}),
                         dash: OmniError("not available")}
    item = extract(context.source_url, context)[0]
    assert item.headers == {} and item.formats[0].headers == {}
    assert item.source_url == context.source_url
