from copy import deepcopy
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.extractors import soundcloud
from omni_dlp.models import OmniError

PAGE = "https://soundcloud.com/artist/song"
SET = "https://soundcloud.com/artist/sets/collection"
CLIENT = "f" * 32
CDN = "https://cf-media.sndcdn.com/fixture/"
API = "https://api-v2.soundcloud.com/media/soundcloud:tracks:1/fixture/stream/"


def track(identifier=1, **changes):
    data = {"id": identifier, "kind": "track", "title": "Song " + str(identifier), "sharing": "public",
            "policy": "ALLOW", "streamable": True, "duration": 180000,
            "permalink_url": PAGE if identifier == 1 else PAGE + str(identifier),
            "user": {"id": 9, "username": "Fixture Artist"},
            "artwork_url": "https://i1.sndcdn.com/artworks-fixture-large.jpg",
            "media": {"transcodings": [{"url": API + "progressive", "preset": "mp3_128k", "snipped": False,
                "format": {"protocol": "progressive", "mime_type": "audio/mpeg"}, "quality": "sq"}]}}
    data.update(changes)
    return data


def html(resource=None, client=True, scripts=""):
    hydration = []
    if client:
        hydration.append({"hydratable": "apiClient", "data": {"id": CLIENT}})
    if resource is not None:
        hydration.append({"hydratable": "playlist" if resource.get("kind") == "playlist" else "sound", "data": resource})
    return "<html><script>window.__sc_hydration = " + json.dumps(hydration) + ";</script>" + scripts + "</html>"


def playlist(tracks=None, **changes):
    data = {"kind": "playlist", "id": 100, "sharing": "public", "title": "Collection", "permalink_url": SET,
            "tracks": tracks if tracks is not None else [track()], "track_count": len(tracks) if tracks is not None else 1}
    data.update(changes)
    return data


class Context(ExtractorContext):
    def __init__(self, page, replies=None, *, source=PAGE, final=None, extra_documents=None, **kwargs):
        metadata_only = kwargs.pop("metadata_only", False)
        super().__init__(source_url=source, **kwargs)
        self.metadata_only = metadata_only
        self.documents = {source: (page.encode(), final or source)}
        self.documents.update(extra_documents or {})
        self.replies = deepcopy(replies or [])
        self.calls = []

    def get_document(self, url, **kwargs):
        self.calls.append(("document", url))
        data, final = self.documents[url]
        return data, final, self.scoped_headers(final)

    def get_json(self, url):
        self.calls.append(("json", url))
        if not self.replies:
            raise AssertionError("unexpected API request")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.mark.parametrize("url", [PAGE, SET, "http://m.soundcloud.com/user/track", "https://on.soundcloud.com/abc123"])
def test_supported_public_urls(url):
    assert soundcloud.supports(url)


@pytest.mark.parametrize("url", ["https://soundcloud.com.evil.invalid/a/b", "https://evil.soundcloud.com/a/b",
    "https://soundcloud.com:8443/a/b", "file:///artist/song", "https://user:secret@soundcloud.com/a/b",
    "https://soundcloud.com/artist", "https://soundcloud.com/discover/sets", PAGE + "/s-private",
    PAGE + "?secret_token=s-private", PAGE + "?oauth_token=private", "https://soundcloud.com/artist/tracks"])
def test_rejects_private_unsupported_and_unowned_urls(url):
    assert not soundcloud.supports(url)


def test_progressive_track_metadata_and_scoped_headers():
    context = Context(html(track()), [{"url": CDN + "song.mp3?signature=fixture"}],
                      headers={"Authorization": "fixture-secret", "X-Api-Key": "fixture-secret"})
    [media] = soundcloud.extract(PAGE, context)
    assert media.kind == "audio" and media.ext == "mp3" and media.id == "1"
    assert media.metadata["archive_id"] == "soundcloud:1" and media.metadata["duration"] == 180
    assert media.metadata["uploader"] == "Fixture Artist"
    assert media.formats[0].format_id == "sc-progressive-mp3_128k"
    assert media.formats[0].vcodec == "none" and media.formats[0].abr == 128
    assert media.formats[0].protocol == "http"
    assert "Authorization" not in media.headers and "X-Api-Key" not in media.headers
    assert parse_qs(urlsplit(context.calls[1][1]).query)["client_id"] == [CLIENT]
    assert media.source_url == PAGE
    assert "client_id" not in json.dumps(media.to_dict())


def test_hls_and_new_soundcloud_cloud_host():
    value = track()
    value["media"]["transcodings"] = [{"url": API + "hls", "preset": "aac_160k", "format": {
        "protocol": "hls", "mime_type": 'audio/mp4; codecs="mp4a.40.2"'}, "quality": "sq"}]
    context = Context(html(value), [{"url": "https://playback.media-streaming.soundcloud.cloud/fixture/playlist.m3u8"}])
    [media] = soundcloud.extract(PAGE, context)
    assert media.ext == "m4a" and media.formats[0].protocol == "hls"
    assert media.formats[0].acodec == "mp4a.40.2" and media.formats[0].abr == 160


def test_duplicate_streams_and_selector_safe_preset_ids():
    value = track()
    value["media"]["transcodings"] = [deepcopy(value["media"]["transcodings"][0]) for _ in range(3)]
    for entry in value["media"]["transcodings"]:
        entry["preset"] = "best / a+b,[x]"
    context = Context(html(value), [{"url": CDN + "one.mp3"}, {"url": CDN + "one.mp3"}, {"url": CDN + "two.mp3"}])
    [media] = soundcloud.extract(PAGE, context)
    assert len(media.formats) == 2
    import re
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", f.format_id) for f in media.formats)
    assert len({f.format_id for f in media.formats}) == 2
    assert media.formats[0].metadata["preset"] == "best / a+b,[x]"


@pytest.mark.parametrize("changes", [{"sharing": "private"}, {"secret_token": "private"},
    {"policy": "SNIP"}, {"policy": "BLOCK"}, {"streamable": False}])
def test_restricted_track_does_not_request_playback(changes):
    context = Context(html(track(**changes)))
    with pytest.raises(OmniError):
        soundcloud.extract(PAGE, context)
    assert len(context.calls) == 1


def test_monetized_public_track_is_accepted():
    context = Context(html(track(policy="MONETIZE")), [{"url": CDN + "song.mp3"}])
    assert soundcloud.extract(PAGE, context)[0].id == "1"


def test_previews_and_protected_protocols_skipped_with_diagnostics():
    value = track()
    good = value["media"]["transcodings"][0]
    value["media"]["transcodings"] = [dict(good, snipped=True),
        dict(good, format={"protocol": "ctr-hls"}), good]
    context = Context(html(value), [{"url": CDN + "song.mp3"}])
    [media] = soundcloud.extract(PAGE, context)
    assert len(media.formats) == 1 and len(media.metadata["format_warnings"]) == 2
    assert len(context.calls) == 2


@pytest.mark.parametrize("bad", ["file:///secret", "https://attacker.invalid/song.mp3", "https://cf-media.sndcdn.com:8443/song.mp3"])
def test_unsafe_playback_urls_are_not_formats(bad):
    with pytest.raises(OmniError, match="no full publicly playable"):
        soundcloud.extract(PAGE, Context(html(track()), [{"url": bad}]))


def test_no_client_id_is_sent_to_unowned_api_endpoint():
    value = track()
    value["media"]["transcodings"][0]["url"] = "https://attacker.invalid/media/stream"
    context = Context(html(value))
    with pytest.raises(OmniError, match="no full publicly playable"):
        soundcloud.extract(PAGE, context)
    assert len(context.calls) == 1


def test_short_link_uses_final_page_and_original_credential_origin():
    source = "https://on.soundcloud.com/abc123"
    context = Context(html(track()), [{"url": CDN + "song.mp3"}], source=source, final=PAGE)
    [media] = soundcloud.extract(source, context)
    assert media.source_url == source and media.metadata["webpage_url"] == PAGE


def test_artwork_only_requires_no_client_id_or_playback_request():
    context = Context(html(track(), client=False), media_types={"image"})
    [media] = soundcloud.extract(PAGE, context)
    assert media.kind == "image" and media.id == "1-artwork"
    assert media.metadata["archive_id"] == "soundcloud:1:artwork"
    assert len(context.calls) == 1


def test_irrelevant_media_filter_does_not_fetch_page():
    context = Context("", media_types={"video"})
    assert soundcloud.extract(PAGE, context) == []
    assert context.calls == []


def test_client_discovery_from_owned_script_without_execution():
    asset = "https://a-v2.sndcdn.com/assets/app.js"
    page = html(track(), client=False, scripts='<script src="https://attacker.invalid/evil.js"></script><script src="'+asset+'"></script>')
    context = Context(page, [{"url": CDN + "song.mp3"}], extra_documents={asset:(('var config={client_id:"'+CLIENT+'"}; throw new Error("do not execute")').encode(),asset)})
    [media] = soundcloud.extract(PAGE, context)
    assert media.kind == "audio"
    assert not any("attacker.invalid" in url for _, url in context.calls)


def test_resolve_fallback_and_malformed_hydration():
    context = Context(html(), [track(), {"url": CDN + "song.mp3"}])
    assert soundcloud.extract(PAGE, context)[0].id == "1"
    assert urlsplit(context.calls[1][1]).path == "/resolve"
    with pytest.raises(OmniError, match="malformed"):
        soundcloud.extract(PAGE, Context('<script>window.__sc_hydration = [broken</script>'))


def test_missing_public_client_is_actionable():
    with pytest.raises(OmniError, match="public playback client"):
        soundcloud.extract(PAGE, Context(html(track(), client=False)))


def test_playlist_enriches_id_stubs_in_order_and_reports_complete():
    data = playlist([track(), {"id": 2}])
    context = Context(html(data), [[track(2)], {"url": CDN + "one.mp3"}, {"url": CDN + "two.mp3"}], source=SET)
    media = soundcloud.extract(SET, context)
    assert [m.id for m in media] == ["1", "2"]
    assert [m.metadata["playlist_index"] for m in media] == [1, 2]
    assert all(m.metadata["playlist_count"] == 2 and m.metadata["collection_complete"] for m in media)
    assert parse_qs(urlsplit(context.calls[1][1]).query)["ids"] == ["2"]


def test_playlist_limit_does_not_fetch_unselected_stubs():
    context = Context(html(playlist([track(), {"id": 2}, {"id": 3}])), [{"url": CDN + "one.mp3"}], source=SET, max_items=1)
    [media] = soundcloud.extract(SET, context)
    assert media.metadata["limited"] is True and not media.metadata["collection_complete"]
    assert len(context.calls) == 2


def test_playlist_missing_and_restricted_tracks_remain_incomplete():
    data = playlist([track(), {"id": 2}, track(3, policy="BLOCK")])
    context = Context(html(data), [[], {"url": CDN + "one.mp3"}], source=SET)
    [media] = soundcloud.extract(SET, context)
    assert media.metadata["unavailable_track_ids"] == ["2", "3"]
    assert not media.metadata["collection_complete"]


def test_playlist_refresh_when_page_metadata_is_truncated():
    initial = playlist([track()], track_count=2)
    refreshed = playlist([track(), track(2)])
    context = Context(html(initial), [refreshed, {"url": CDN + "one.mp3"}, {"url": CDN + "two.mp3"}], source=SET)
    assert len(soundcloud.extract(SET, context)) == 2
    assert urlsplit(context.calls[1][1]).path == "/playlists/100"


def test_empty_playlist_is_empty_and_missing_nonempty_playlist_fails():
    assert soundcloud.extract(SET, Context(html(playlist([])), source=SET)) == []
    data = playlist([], track_count=2)
    with pytest.raises(OmniError, match="no available"):
        soundcloud.extract(SET, Context(html(data), [data], source=SET))


def test_playlist_asset_limit_reports_omitted_artwork():
    context = Context(html(playlist([track()])), [{"url": CDN + "song.mp3"}], source=SET,
                      media_types={"audio", "image"}, max_items=1)
    [media] = soundcloud.extract(SET, context)
    assert media.metadata["limited"] is True and not media.metadata["collection_complete"]


@pytest.mark.parametrize("value", ["NaN", 2.5, -1, True, {}])
def test_invalid_playlist_count_cannot_claim_complete(value):
    context = Context(html(playlist([track()], track_count=value)), source=SET)
    with pytest.raises(OmniError, match="track count"):
        soundcloud.extract(SET, context)


def test_complete_playlist_artwork_does_not_need_client_discovery():
    data = playlist([track(), track(2)])
    context = Context(html(data, client=False), source=SET, media_types={"image"})
    items = soundcloud.extract(SET, context)
    assert [item.id for item in items] == ["1-artwork", "2-artwork"]
    assert all(item.metadata["collection_complete"] for item in items)
    assert len(context.calls) == 1


@pytest.mark.parametrize("changes", [{"policy": "BLOCK"}, {"streamable": False}, {"media": {}}, {"artwork_url": None, "media": {}}])
def test_public_metadata_only_needs_no_playback_or_client(changes):
    context = Context(html(track(**changes), client=False), metadata_only=True)
    [item] = soundcloud.extract(PAGE, context)
    assert item.url == PAGE and item.source_url == PAGE and item.kind == "audio"
    assert item.formats == [] and item.metadata["formats_required"] and item.metadata["metadata_only"]
    assert item.metadata["archive_id"] == "soundcloud:1" and len(context.calls) == 1


def test_metadata_only_never_allows_private_tracks():
    context = Context(html(track(sharing="private"), client=False), metadata_only=True)
    with pytest.raises(OmniError, match="Private"):
        soundcloud.extract(PAGE, context)


def test_metadata_only_artwork_remains_directly_downloadable():
    context = Context(html(track(policy="BLOCK"), client=False), metadata_only=True, media_types={"audio", "image"})
    audio, artwork = soundcloud.extract(PAGE, context)
    assert audio.metadata["formats_required"]
    assert artwork.kind == "image" and "formats_required" not in artwork.metadata
    assert "metadata_only" not in artwork.metadata


def test_metadata_only_set_with_full_metadata_needs_no_client():
    value = track(media={}, artwork_url=None, policy="BLOCK")
    context = Context(html(playlist([value]), client=False), source=SET, metadata_only=True)
    [item] = soundcloud.extract(SET, context)
    assert item.metadata["collection_complete"] and item.metadata["formats_required"]
    assert len(context.calls) == 1


@pytest.mark.parametrize("changes", [{"sharing": []}, {"policy": {}}, {"kind": []}])
def test_malformed_resource_flags_are_domain_errors(changes):
    with pytest.raises(OmniError):
        soundcloud.extract(PAGE, Context(html(track(**changes))))


@pytest.mark.parametrize("field,value", [("url", "https://[broken/"), ("format", {"protocol": []})])
def test_malformed_transcoding_is_not_a_python_exception(field, value):
    resource = track()
    resource["media"]["transcodings"][0][field] = value
    with pytest.raises(OmniError, match="no full publicly playable"):
        soundcloud.extract(PAGE, Context(html(resource)))


def test_encoded_path_separators_rejected_and_set_slug_is_not_private_token():
    assert not soundcloud.supports(PAGE + "%2Fs-private")
    assert not soundcloud.supports("https://soundcloud.com/artist/%2e%2e")
    assert soundcloud.supports("https://soundcloud.com/artist/sets/s-album")
