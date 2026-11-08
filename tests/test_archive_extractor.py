from copy import deepcopy
import re

import pytest

from omni_dlp.extractors.archive import extract, supports
from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.models import OmniError


class FixtureContext(ExtractorContext):
    def __init__(self, data, url="https://archive.org/details/fixture", **kwargs):
        super().__init__(source_url=url, **kwargs)
        self.data = data
        self.requested = []

    def get_json(self, url):
        self.requested.append(url)
        return deepcopy(self.data)


def item(files=None, **metadata):
    return {"metadata": {"title": "Mixed fixture", "creator": ["Example creator"], **metadata}, "files": files or [
        {"name": "track.flac", "source": "original", "format": "Flac", "size": "96000", "length": "00:03", "title": "A track"},
        {"name": "track.mp3", "source": "derivative", "original": "track.flac", "format": "MP3", "size": "24000", "length": "3.0"},
        {"name": "track.ogg", "source": "derivative", "original": "track.mp3", "format": "Ogg Vorbis", "size": "12000", "length": "3.0"},
        {"name": "track.png", "source": "derivative", "original": "track.flac", "format": "PNG", "size": "100"},
        {"name": "notes.pdf", "source": "original", "format": "Text PDF", "size": "456"},
        {"name": "images.zip", "source": "original", "format": "ZIP", "size": "1000"},
        {"name": "fixture_meta.xml", "source": "original", "format": "Metadata"},
        {"name": "fixture_archive.torrent", "source": "metadata", "format": "Archive BitTorrent"},
    ]}


@pytest.mark.parametrize("url", [
    "https://archive.org/details/fixture", "http://www.archive.org/embed/fixture",
    "https://archive.org/download/fixture/track.mp3", "https://archive.org/details/item?x=y",
    "https://archive.org/download/fixture/folder/a%20b.flac",
])
def test_recognizes_supported_urls(url):
    assert supports(url)


@pytest.mark.parametrize("url", [
    "https://archive.org.evil.test/details/fixture", "file:///details/fixture", "https://web.archive.org/web/123/test",
    "https://archive.org/metadata/fixture", "https://user:password@archive.org/details/fixture",
    "https://archive.org/details/%2Fetc", "https://archive.org/download/fixture/../secret",
    "https://archive.org/download/fixture/%2e%2e/secret", "https://archive.org/details/..",
    "https://archive.org/details/fixture/file%00name", "https://archive.org:5000/details/fixture",
])
def test_rejects_unsafe_or_out_of_scope_urls(url):
    assert not supports(url)


def test_groups_audio_derivatives_but_keeps_other_media_distinct():
    context = FixtureContext(item())
    media = extract(context.source_url, context)
    assert [m.kind for m in media] == ["audio", "image", "document", "archive"]
    audio = media[0]
    assert audio.title == "A track"
    assert [f.format_id for f in audio.formats] == ["track.flac", "track.mp3", "track.ogg"]
    assert audio.formats[0].vcodec == "none"
    assert audio.formats[0].acodec == "flac"
    assert audio.formats[0].tbr == 256
    assert audio.formats[0].filesize == 96000
    assert audio.metadata["duration"] == 3
    assert audio.metadata["omitted_files"] == {"private": 0, "metadata": 2, "invalid": 0}
    assert audio.metadata["archive_id"] == "internetarchive:fixture/track.flac"
    assert audio.source_url == context.source_url
    assert all(not m.formats for m in media[1:])
    assert context.requested == ["https://archive.org/metadata/fixture?extended_err=1"]


def test_exact_file_url_selects_requested_derivative_only():
    context = FixtureContext(item(), "https://archive.org/download/fixture/track.mp3")
    media = extract(context.source_url, context)
    assert len(media) == 1 and len(media[0].formats) == 1
    assert media[0].formats[0].format_id == "track.mp3"
    assert media[0].url.endswith("/track.mp3")
    assert media[0].metadata["archive_id"] == "internetarchive:fixture/track.mp3"


def test_exact_metadata_file_can_be_downloaded_explicitly():
    context = FixtureContext(item(), "https://archive.org/download/fixture/fixture_meta.xml")
    media = extract(context.source_url, context)
    assert len(media) == 1 and media[0].ext == "xml"


def test_unicode_nested_filenames_are_safely_encoded():
    context = FixtureContext(item([{"name": "folder/été ?#.mp3", "source": "original"}]))
    media = extract(context.source_url, context)[0]
    assert media.url == "https://archive.org/download/fixture/folder/%C3%A9t%C3%A9%20%3F%23.mp3"


def test_filter_and_max_items_are_explicit_in_metadata():
    context = FixtureContext(item(), media_types={"audio", "document"}, max_items=1)
    media = extract(context.source_url, context)
    assert len(media) == 1 and media[0].kind == "audio"
    assert media[0].metadata["playlist_count"] == 2
    assert media[0].metadata["discovery_limited"] is True


@pytest.mark.parametrize("data,message", [
    ([], "not found"), ({"error": "server issue"}, "unavailable"), ([1], "invalid item record"),
    ({"metadata": [], "files": []}, "invalid item metadata"),
    ({"metadata": {}, "files": {}}, "valid file list"),
    ({"is_dark": True, "metadata": {}, "files": []}, "private"),
    (item(**{"access-restricted-item": "true"}), "access-restricted"),
    (item(mediatype="collection"), "collection traversal"),
])
def test_unavailable_and_unsupported_items_fail_actionably(data, message):
    context = FixtureContext(data)
    with pytest.raises(OmniError, match=message):
        extract(context.source_url, context)


def test_private_files_are_counted_and_requested_private_file_fails():
    data = item([{"name": "private.mp3", "private": "true"}, {"name": "public.mp3"}])
    context = FixtureContext(data)
    media = extract(context.source_url, context)
    assert len(media) == 1 and media[0].metadata["omitted_files"]["private"] == 1
    context.source_url = "https://archive.org/download/fixture/private.mp3"
    with pytest.raises(OmniError, match="file is private"):
        extract(context.source_url, context)


def test_invalid_files_are_omitted_without_external_urls_or_traversal():
    context = FixtureContext(item([None, {"name": "../secret.mp3"}, {"name": "/secret.mp3"},
                                   {"name": "good.mp3", "source": "original"}]))
    media = extract(context.source_url, context)
    assert len(media) == 1 and media[0].metadata["omitted_files"]["invalid"] == 3


def test_conflicting_duplicates_fail_instead_of_silently_losing_data():
    context = FixtureContext(item([{"name": "same.mp3", "size": "1"}, {"name": "same.mp3", "size": "2"}]))
    with pytest.raises(OmniError, match="conflicting"):
        extract(context.source_url, context)


def test_derivative_cycles_do_not_hang_or_drop_files():
    context = FixtureContext(item([{"name": "a.mp3", "original": "b.mp3"}, {"name": "b.mp3", "original": "a.mp3"}]))
    media = extract(context.source_url, context)
    assert len(media) == 2
    assert {m.url.rsplit("/", 1)[-1] for m in media} == {"a.mp3", "b.mp3"}


def test_video_ogg_and_invalid_numeric_metadata():
    context = FixtureContext(item([{"name": "movie.ogg", "format": "Ogg Theora", "height": "720", "width": "1280",
                                   "size": "NaN", "length": "Inf", "fps": True, "bitrate": "-3"}]))
    video = extract(context.source_url, context)[0]
    assert video.kind == "video" and video.formats[0].vcodec == ""
    assert video.formats[0].height == 720 and video.formats[0].width == 1280
    assert video.formats[0].filesize is None and video.formats[0].fps is None and video.formats[0].tbr is None


def test_credentials_are_scoped_to_initial_origin():
    context = FixtureContext(item(), "https://www.archive.org/details/fixture", headers={"Authorization": "secret", "X-Token": "secret"})
    media = extract(context.source_url, context)[0]
    assert not any(k.lower() in {"authorization", "x-token"} for k in media.headers)
    assert media.source_url == context.source_url


def test_repeated_extraction_has_stable_ids():
    context = FixtureContext(item())
    a = extract(context.source_url, context)
    b = extract(context.source_url, context)
    assert [m.id for m in a] == [m.id for m in b]
    assert len({m.id for m in a}) == len(a)


def test_explicit_missing_file_fails():
    context = FixtureContext(item(), "https://archive.org/download/fixture/missing.mp3")
    with pytest.raises(OmniError, match="not found"):
        extract(context.source_url, context)


@pytest.mark.parametrize("filename", ["folder/a.mp3", "a + b.mp3", "a,b.mp3", "é[1].mp3", "best", "mp4"])
def test_format_ids_are_safe_selector_tokens(filename):
    context = FixtureContext(item([{"name": filename, "source": "original"}]))
    media = extract(context.source_url, context)[0]
    # Extensionless files have no AV candidate list, but helper identifiers must
    # still be safe if future type metadata supplies an audio/video category.
    from omni_dlp.extractors.archive import _format_id
    token = media.formats[0].format_id if media.formats else _format_id(filename, media.ext)
    assert re.fullmatch(r"[A-Za-z0-9_.-]+", token)
    assert token.startswith("file-") and token != filename
    if media.formats:
        assert media.formats[0].metadata["filename"] == filename
