from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

import pytest

from omni_dlp.extractors import wikimedia
from omni_dlp.extractors.common import ExtractorContext
from omni_dlp.models import OmniError

BASE = "https://commons.wikimedia.org/wiki/"
UPLOAD = "https://upload.wikimedia.org/wikipedia/commons/0/00/"


class Context(ExtractorContext):
    def __init__(self, replies, source=BASE + "File:Example.jpg", **kwargs):
        super().__init__(source_url=source, **kwargs)
        self.replies = deepcopy(replies)
        self.calls = []

    def get_json(self, url):
        self.calls.append(url)
        if not self.replies:
            raise AssertionError("unexpected API request")
        return self.replies.pop(0)


def file_page(name="Example.jpg", pageid=1, mediatype="BITMAP", mime="image/jpeg", **info):
    return {"pageid": pageid, "ns": 6, "title": "File:" + name,
            "videoinfo": [{"url": UPLOAD + name, "mime": mime,
                "mediatype": mediatype, "size": 1234, "width": 100, "height": 80,
                "sha1": "a" * 40, **info}]}


def pages(*items, **extra):
    return {"query": {"pages": list(items)}, **extra}


def category(*titles, continuation=None):
    result = {"query": {"categorymembers": [{"ns": 6, "title": title} for title in titles]}}
    if continuation is not None:
        result["continue"] = {"cmcontinue": continuation}
    return result


@pytest.mark.parametrize("url", [BASE + "File:Example.jpg", BASE + "Image:Example.jpg",
    BASE + "Category:Audio", "https://en.wikipedia.org/wiki/File:Example.jpg",
    "https://commons.m.wikimedia.org/wiki/File:Example.jpg",
    "https://fr.m.wikipedia.org/w/index.php?title=File%3AExample.jpg",
    "https://www.mediawiki.org/wiki/File:Example.svg"])
def test_supported_urls(url):
    assert wikimedia.supports(url)


@pytest.mark.parametrize("url", ["https://commons.wikimedia.org.evil.invalid/wiki/File:A.jpg",
    "https://evil.wikimedia.org/wiki/File:A.jpg", "https://commons.wikimedia.org:9000/wiki/File:A.jpg",
    "file:///wiki/File:A.jpg", "https://user:secret@commons.wikimedia.org/wiki/File:A.jpg",
    "https://commons.wikimedia.org/wiki/Some_article", BASE + "File:A|File:B",
    BASE + "File:", "https://commons.wikimedia.org/w/index.php?title=Main_Page"])
def test_rejects_unowned_hosts_credentials_non_files(url):
    assert not wikimedia.supports(url)


def test_image_is_original_not_scaled_thumbnail():
    context = Context([pages(file_page())], headers={"Authorization": "private", "X-Api-Key": "private"})
    [media] = wikimedia.extract(BASE + "File:Example.jpg", context)
    assert media.kind == "image" and media.ext == "jpg"
    assert media.title == "Example" and media.id == "1"
    assert media.url == UPLOAD + "Example.jpg"
    assert media.formats == []
    assert media.metadata["archive_id"] == "wikimedia:commons.wikimedia.org:1"
    assert media.metadata["source_sha1"] == "a" * 40
    assert media.source_url == context.source_url
    assert "Authorization" not in media.headers and "X-Api-Key" not in media.headers
    query = parse_qs(urlsplit(context.calls[0]).query)
    assert query["prop"] == ["videoinfo"] and query["vilimit"] == ["1"]


def test_audio_has_original_and_derivatives_and_subtitle_metadata():
    page = file_page("Song.ogg", mediatype="AUDIO", mime="application/ogg", duration=10,
        derivatives=[{"src": UPLOAD + "Song.mp3", "type": "audio/mpeg", "transcodekey": "mp3",
                      "bandwidth": 128000},
                     {"src": UPLOAD + "Song.mp3", "type": "audio/mpeg", "transcodekey": "duplicate"}],
        timedtext=[{"src": "https://commons.wikimedia.org/w/api.php?action=timedtext", "srclang": "en", "type": "text/vtt"}])
    [media] = wikimedia.extract(BASE + "File:Song.ogg", Context([pages(page)]))
    assert media.kind == "audio" and media.formats[0].vcodec == "none"
    assert [f.format_id for f in media.formats] == ["original", "transcode-mp3"]
    assert media.formats[1].abr == 128 and media.formats[0].filesize == 1234
    assert media.metadata["duration"] == 10
    assert media.metadata["subtitles"]["en"][0]["ext"] == "vtt"


def test_video_codec_dimensions_and_no_invalid_numbers():
    page = file_page("Movie.webm", mediatype="VIDEO", mime="video/webm", duration="nan",
        derivatives=[{"src": UPLOAD + "Movie.360p.webm", "transcodekey": "360p.vp9.webm",
                      "type": 'video/webm; codecs="vp9, opus"', "width": 640, "height": 360,
                      "framerate": 24, "bandwidth": 400000}])
    [media] = wikimedia.extract(BASE + "File:Movie.webm", Context([pages(page)]))
    selected = media.formats[1]
    assert (selected.vcodec, selected.acodec, selected.width, selected.height) == ("vp9", "opus", 640, 360)
    assert selected.tbr == 400 and selected.fps == 24
    assert media.metadata["duration"] is None


def test_document_original_and_type_filter():
    page = file_page("Book.pdf", mediatype="OFFICE", mime="application/pdf")
    [media] = wikimedia.extract(BASE + "File:Book.pdf", Context([pages(page)]))
    assert media.kind == "document" and media.ext == "pdf"
    assert wikimedia.extract(BASE + "File:Book.pdf", Context([pages(page)], media_types={"audio"})) == []


def test_unicode_titles_and_mobile_normalization():
    title = "File:Café_example.jpg"
    url = "https://commons.m.wikimedia.org/wiki/File:Caf%C3%A9_example.jpg"
    [media] = wikimedia.extract(url, Context([pages(file_page("Café example.jpg"))], source=url))
    assert media.title == "Café example"
    assert "%C3%A9%20" in media.metadata["webpage_url"]
    assert media.source_url == url
    assert media.metadata["archive_id"].startswith("wikimedia:commons.wikimedia.org:")
    context = Context([pages(file_page("Café example.jpg"))], source=url)
    wikimedia.extract(url, context)
    assert parse_qs(urlsplit(context.calls[0]).query)["titles"] == [title.replace("_", " ")]


def test_shared_file_with_missing_local_description_is_accepted():
    page = file_page()
    page["missing"] = True
    page["imagerepository"] = "shared"
    [media] = wikimedia.extract(BASE + "File:Example.jpg", Context([pages(page)]))
    assert media.url == UPLOAD + "Example.jpg"


@pytest.mark.parametrize("response", [[], {}, {"error": {"info": "private detail"}},
    pages({"title": "File:Missing.jpg", "missing": True}), pages(file_page(filehidden=True)),
    pages(file_page(url="file:///etc/passwd")), pages(file_page(url="https://attacker.invalid/file.jpg")),
    pages(file_page(derivatives="malformed", mediatype="VIDEO", mime="video/webm"))])
def test_invalid_hidden_or_unsafe_api_results_fail_closed(response):
    with pytest.raises(OmniError) as error:
        wikimedia.extract(BASE + "File:Example.jpg", Context([response]))
    assert "private detail" not in str(error.value)


def test_fallback_when_project_has_no_timedmediahandler():
    first = pages({"title": "File:Example.jpg"}, warnings={"query": {"warnings": "unknown videoinfo"}})
    second = file_page()
    second["imageinfo"] = second.pop("videoinfo")
    context = Context([first, pages(second)])
    [media] = wikimedia.extract(BASE + "File:Example.jpg", context)
    assert media.kind == "image"
    assert parse_qs(urlsplit(context.calls[-1]).query)["prop"] == ["imageinfo"]


def test_category_pagination_and_file_order():
    context = Context([
        category("File:B.jpg", "File:A.jpg", continuation="next"),
        pages(file_page("A.jpg", 1), file_page("B.jpg", 2)),
        category("File:C.jpg"), pages(file_page("C.jpg", 3)),
    ])
    media = wikimedia.extract(BASE + "Category:Examples", context)
    assert [item.title for item in media] == ["B", "A", "C"]
    assert [item.metadata["playlist_index"] for item in media] == [1, 2, 3]
    assert all(item.metadata["collection_complete"] for item in media)
    assert parse_qs(urlsplit(context.calls[2]).query)["cmcontinue"] == ["next"]
    assert all(item.source_url == BASE + "Category:Examples" for item in media)


def test_category_limit_and_inaccessible_member_are_explicit():
    context = Context([category("File:A.jpg", "File:B.jpg", continuation="next"),
        pages(file_page("A.jpg", 1), {"title": "File:B.jpg", "missing": True})], max_items=2)
    [media] = wikimedia.extract(BASE + "Category:Examples", context)
    assert media.metadata["collection_truncated"] is True
    assert media.metadata["collection_skipped"] == 1
    assert media.metadata["collection_complete"] is False
    assert len(context.calls) == 2


def test_category_repeated_continuation_rejected():
    context = Context([category("File:A.jpg", continuation="same"), pages(file_page("A.jpg", 1)),
                       category("File:B.jpg", continuation="same"), pages(file_page("B.jpg", 2))])
    with pytest.raises(OmniError, match="pagination"):
        wikimedia.extract(BASE + "Category:Examples", context)


def test_category_missing_batch_records_is_not_complete():
    context = Context([category("File:A.jpg", "File:B.jpg"), pages(file_page("A.jpg", 1))])
    with pytest.raises(OmniError, match="incomplete"):
        wikimedia.extract(BASE + "Category:Examples", context)


def test_category_empty_is_empty_and_all_unavailable_fails():
    assert wikimedia.extract(BASE + "Category:Empty", Context([category()])) == []
    context = Context([category("File:Missing.jpg"), pages({"title": "File:Missing.jpg", "missing": True})])
    with pytest.raises(OmniError, match="unavailable"):
        wikimedia.extract(BASE + "Category:Missing", context)


def test_category_redirect_aliases_coalesce_without_false_incomplete_error():
    response = pages(file_page("Canonical.jpg", 42))
    response["query"]["redirects"] = [
        {"from": "File:A.jpg", "to": "File:Canonical.jpg"},
        {"from": "File:B.jpg", "to": "File:Canonical.jpg"}]
    context = Context([category("File:A.jpg", "File:B.jpg"), response])
    [media] = wikimedia.extract(BASE + "Category:Examples", context)
    assert media.id == "42" and media.metadata["collection_complete"] is True


def test_file_redirect_cycle_rejected():
    response = pages(file_page("A.jpg"))
    response["query"]["redirects"] = [{"from": "File:A.jpg", "to": "File:B.jpg"},
                                       {"from": "File:B.jpg", "to": "File:A.jpg"}]
    with pytest.raises(OmniError, match="cyclic"):
        wikimedia.extract(BASE + "File:A.jpg", Context([response]))


def test_rendition_ids_are_selector_safe_and_preserve_raw_keys():
    keys = ["mp4", "all", "best", "360p/vp9 + audio,[a]", "a b", "a_b"]
    page = file_page("Movie.webm", mediatype="VIDEO", mime="video/webm",
        derivatives=[{"src": UPLOAD + str(i) + ".webm", "type": "video/webm", "transcodekey": key}
                     for i, key in enumerate(keys)])
    [media] = wikimedia.extract(BASE + "File:Movie.webm", Context([pages(page)]))
    import re
    ids = [f.format_id for f in media.formats]
    assert len(ids) == len(set(ids))
    assert all(re.fullmatch(r"[A-Za-z0-9_.-]+", format_id) for format_id in ids)
    assert [f.metadata["transcode_key"] for f in media.formats[1:]] == keys
    assert "transcode-mp4" in ids and "transcode-all" in ids
