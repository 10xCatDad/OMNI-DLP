from __future__ import annotations

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import hashlib
import json
import threading
from unittest.mock import patch

import pytest

from omni_dlp.assets import AssetOptions, AssetPlan, download_assets, plan_assets
from omni_dlp.models import Media, OmniError
from omni_dlp.transfer import TransferOptions


def track(language="en", extension="vtt", *, automatic=False, **kwargs):
    return {"url": f"https://captions.example/{'auto-' if automatic else ''}{language}.{extension}", "ext": extension, **kwargs}


def media(**metadata):
    return Media("https://video.example/video.mp4", title="Example", id="example", kind="video", ext="mp4",
                 source_url="https://page.example/watch/1", metadata=metadata)


def plans(tmp_path, opts=None, item=None, filename="video.mp4"):
    return plan_assets(item or media(subtitles={"en": [track()]}), tmp_path / filename, opts or AssetOptions(write_subs=True))


def test_disabled_options_and_missing_metadata_do_not_create_files(tmp_path):
    assert plans(tmp_path, AssetOptions()) == []
    assert plans(tmp_path, AssetOptions(write_subs=True, write_thumbnail=True, write_description=True), media()) == []
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("manual,automatic,expected", [
    ({"fr": [track("fr")], "en": [track()]}, {"en-US": [track("en-US", automatic=True)]}, "en"),
    ({"fr": [track("fr")], "en-GB": [track("en-GB")]}, {"en": [track(automatic=True)]}, "en-GB"),
    ({"fr": [track("fr")]}, {"en": [track(automatic=True)]}, "en"),
    ({"fr": [track("fr")]}, {"ja": [track("ja", automatic=True)]}, "fr"),
    ({}, {"ja": [track("ja", automatic=True)]}, "ja"),
])
def test_default_single_language_priority(tmp_path, manual, automatic, expected):
    selected = plans(tmp_path, AssetOptions(write_subs=True, write_auto_subs=True),
                     media(subtitles=manual, automatic_captions=automatic))
    assert [plan.language for plan in selected] == [expected]


def test_manual_precedence_and_auto_only_selection(tmp_path):
    item = media(subtitles={"en": [track()]}, automatic_captions={"en": [track(automatic=True)]})
    manual = plans(tmp_path, AssetOptions(write_subs=True, write_auto_subs=True), item)[0]
    automatic = plans(tmp_path, AssetOptions(write_auto_subs=True), item)[0]
    assert manual.media.url.endswith("/en.vtt") and not manual.media.metadata["automatic"]
    assert automatic.media.url.endswith("/auto-en.vtt") and automatic.media.metadata["automatic"]
    assert manual.media.metadata["resource_id"] != automatic.media.metadata["resource_id"]


@pytest.mark.parametrize("query,expected", [
    ("en.*,ja", ["en", "en-US", "en-GB", "ja"]),
    ("all,-live_chat,-en-GB", ["en", "en-US", "ja"]),
    ("glob:en-*", ["en-US", "en-GB"]),
    ("re:^en-[A-Z]+$", ["en-US", "en-GB"]),
    ("ja,en.*,-en,ja", ["ja", "en-US", "en-GB"]),
    ("all,-all,en", ["en"]),
    ("-en", []),
    ("missing", []),
    ("glob:en-??", ["en-US", "en-GB"]),
])
def test_language_expressions_and_exclusions(tmp_path, query, expected):
    item = media(subtitles={language: [track(language)] for language in ("en", "en-US", "en-GB", "ja", "live_chat")})
    assert [plan.language for plan in plans(tmp_path, AssetOptions(write_subs=True, sub_langs=query), item)] == expected


@pytest.mark.parametrize("query", ["(en+)+$", r"en\1", "en{1,9}", "en|ja", "[abc", "[z-a]", "a**", "", "x" * 129])
def test_unsafe_or_unsupported_language_regex_is_rejected(tmp_path, query):
    with pytest.raises(OmniError, match="language"):
        plans(tmp_path, AssetOptions(write_subs=True, sub_langs=query))


def test_repeated_star_patterns_are_bounded_without_backtracking(tmp_path):
    item = media(subtitles={"e" * 120: [track()]})
    assert plans(tmp_path, AssetOptions(write_subs=True, sub_langs="e*" * 60 + "z"), item) == []


def test_format_slash_fallback_and_source_order(tmp_path):
    item = media(subtitles={"en": [track(extension="srt"), track(), track(extension="vtt", name="new")]})
    preferred = plans(tmp_path, AssetOptions(write_subs=True, sub_format="ass/srt/best"), item)[0]
    assert preferred.path.name == "video.en.srt" and not preferred.media.metadata["format_fallback"]
    fallback = plans(tmp_path, AssetOptions(write_subs=True, sub_format="ass"), item)[0]
    assert fallback.path.name == "video.en.vtt" and fallback.media.metadata["format_fallback"]


def test_thumbnail_best_all_and_image_main_collision(tmp_path):
    item = media(thumbnails=[{"url": "https://img.example/low.jpg", "width": 100, "height": 100, "id": "low"},
                             {"url": "https://img.example/high.webp", "width": 800, "height": 600, "id": "high"}])
    best = plans(tmp_path, AssetOptions(write_thumbnail=True), item)
    assert len(best) == 1 and best[0].path.name == "video.webp"
    all_images = plans(tmp_path, AssetOptions(write_all_thumbnails=True), item)
    assert [plan.path.name for plan in all_images] == ["video.low.jpg", "video.high.webp"]
    collision = plans(tmp_path, AssetOptions(write_thumbnail=True), item, "video.webp")
    assert collision[0].path.name == "video.thumbnail.webp"


def test_languages_and_thumbnail_ids_cannot_escape_directory(tmp_path):
    item = media(subtitles={"../../escape": [track()]}, thumbnails=[{"url": "https://img.example/a.jpg", "id": "../../outside"},
                                                                   {"url": "https://img.example/b.jpg", "id": "second"}])
    selected = plans(tmp_path, AssetOptions(write_subs=True, sub_langs="all", write_all_thumbnails=True), item)
    assert all(plan.path.parent == tmp_path and ".." not in plan.path.name for plan in selected)


def test_casefolded_collisions_and_protected_sidecars_are_rejected(tmp_path):
    with pytest.raises(OmniError, match="overlap"):
        plans(tmp_path, AssetOptions(write_subs=True, sub_langs="all"), media(subtitles={"en": [track()], "EN": [track("EN")]}))
    with pytest.raises(OmniError, match="overlap"):
        plans(tmp_path, AssetOptions(write_subs=True, sub_langs="all"), media(subtitles={"mp4.omni": [track(extension="json")]}))


@pytest.mark.parametrize("metadata,opts", [
    ({"subtitles": {"en": [{"url": "file:///etc/passwd", "ext": "vtt"}]}}, AssetOptions(write_subs=True)),
    ({"subtitles": {"en": [track(), {"url": "javascript:bad", "ext": "ass"}]}}, AssetOptions(write_subs=True)),
    ({"subtitles": {"en": [track(ext="../../txt")]}}, AssetOptions(write_subs=True)),
    ({"subtitles": {"en": "not-list"}}, AssetOptions(write_subs=True)),
    ({"subtitles": {"en": [None]}}, AssetOptions(write_subs=True)),
    ({"subtitles": {str(i): [track()] for i in range(257)}}, AssetOptions(write_subs=True)),
    ({"subtitles": {"en": [track()] * 65}}, AssetOptions(write_subs=True)),
    ({"thumbnails": [{"url": "https://u:p@img.example/a"} ]}, AssetOptions(write_thumbnail=True)),
    ({"thumbnails": [None]}, AssetOptions(write_thumbnail=True)),
    ({"description": {"private": "do-not-print"}}, AssetOptions(write_description=True)),
])
def test_malformed_selected_metadata_is_actionable_and_sanitized(tmp_path, metadata, opts):
    with pytest.raises(OmniError) as error:
        plans(tmp_path, opts, media(**metadata))
    assert "do-not-print" not in str(error.value) and "u:p" not in str(error.value)


def test_explicit_asset_headers_and_parent_credentials_are_scoped(tmp_path):
    item = media(subtitles={"en": [track(headers={"X-Asset-Key": "asset-secret"})]})
    item.headers = {"Authorization": "parent-secret", "X-Api-Key": "parent-secret", "User-Agent": "fixture"}
    selected = plans(tmp_path, item=item)[0]
    assert "Authorization" not in selected.media.headers and "X-Api-Key" not in selected.media.headers
    assert selected.media.headers["X-Asset-Key"] == "asset-secret" and selected.media.headers["User-Agent"] == "fixture"


@pytest.fixture
def web():
    requests = []
    count = {}
    caption = b"WEBVTT\n\n00:00.000 --> 00:01.000\nFixture\n"
    image = b"\xff\xd8\xff" + bytes(range(256))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, dict(self.headers)))
            count[self.path] = count.get(self.path, 0) + 1
            if self.path == "/retry.vtt" and count[self.path] == 1:
                self.send_response(503)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = image if self.path.endswith(".jpg") else caption
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"asset-fixture"')
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests, caption, image
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_actual_sidecar_downloads_retry_verify_and_preserve_main(web, tmp_path):
    base, requests, caption, image = web
    main = tmp_path / "movie.mp4"
    main.write_bytes(b"main-media")
    item = media(subtitles={"en": [{"url": base + "/retry.vtt", "ext": "vtt", "headers": {"X-Asset-Key": "asset-key"}}]},
                 thumbnails=[{"url": base + "/image.jpg"}], description="A description\n")
    selected = plan_assets(item, main, AssetOptions(write_subs=True, write_thumbnail=True, write_description=True))
    options = TransferOptions(directory=str(tmp_path), headers={"Authorization": "parent-secret"}, retries=1)
    results = download_assets(selected, options)
    assert [Path(result.path).read_bytes() for result in results] == [caption, image, b"A description\n"]
    assert main.read_bytes() == b"main-media"
    assert len(requests) == 3
    assert all("Authorization" not in headers for _, headers in requests)
    assert all(headers.get("X-Asset-Key") == "asset-key" for path, headers in requests if path.endswith(".vtt"))
    assert all("X-Asset-Key" not in headers for path, headers in requests if path.endswith(".jpg"))
    assert all(result.sha256 == hashlib.sha256(Path(result.path).read_bytes()).hexdigest() for result in results)
    assert all(result.skipped for result in download_assets(selected, options))
    assert len(requests) == 3
    for result in results:
        marker = Path(result.path + ".omni.json").read_text()
        assert "parent-secret" not in marker and "http" not in marker


def test_modified_description_and_tampered_caption_require_explicit_overwrite(web, tmp_path):
    base, _, _, _ = web
    item = media(subtitles={"en": [{"url": base + "/caption.vtt", "ext": "vtt"}]}, description="Original")
    selected = plans(tmp_path, AssetOptions(write_subs=True, write_description=True), item)
    options = TransferOptions(directory=str(tmp_path))
    download_assets(selected, options)
    selected[0].path.write_bytes(b"tampered")
    with pytest.raises(OmniError, match="integrity"):
        download_assets(selected, options)
    download_assets(selected, replace(options, overwrite=True))
    changed = plans(tmp_path, AssetOptions(write_description=True), replace(item, metadata={"description": "Changed"}))
    with pytest.raises(OmniError, match="matching completion"):
        download_assets(changed, options)
    download_assets(changed, replace(options, overwrite=True))
    assert changed[0].path.read_text() == "Changed"


def test_cancellation_prevents_requests_and_inline_publication(web, tmp_path):
    base, requests, _, _ = web
    event = threading.Event()
    event.set()
    selected = plans(tmp_path, AssetOptions(write_subs=True, write_description=True),
                     media(subtitles={"en": [{"url": base + "/caption.vtt", "ext": "vtt"}]}, description="text"))
    with pytest.raises(OmniError, match="cancelled"):
        download_assets(selected, TransferOptions(directory=str(tmp_path), cancel_event=event))
    assert not requests and not any(plan.path.exists() for plan in selected)


def test_download_preflight_rejects_outside_and_overlap_before_requests(web, tmp_path):
    base, requests, _, _ = web
    selected = plans(tmp_path, item=media(subtitles={"en": [{"url": base + "/caption.vtt", "ext": "vtt"}]}))
    outside = replace(selected[0], path=tmp_path.parent / "outside.vtt")
    with pytest.raises(OmniError, match="escapes"):
        download_assets([outside], TransferOptions(directory=str(tmp_path)))
    same_main = replace(selected[0], path=selected[0].main_path)
    with pytest.raises(OmniError, match="overlap"):
        download_assets([same_main], TransferOptions(directory=str(tmp_path)))
    conflict = replace(selected[0], path=Path(str(selected[0].path) + ".omni.json"))
    with pytest.raises(OmniError, match="overlap"):
        download_assets([selected[0], conflict], TransferOptions(directory=str(tmp_path)))
    assert not requests


def test_existing_symlink_refused_and_inline_size_cap(tmp_path):
    original = tmp_path / "original"
    original.write_text("keep")
    selected = plans(tmp_path, AssetOptions(write_description=True), media(description="replacement"))
    try:
        selected[0].path.symlink_to(original)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(OmniError, match="symbolic-link"):
        download_assets(selected, TransferOptions(directory=str(tmp_path), overwrite=True))
    assert original.read_text() == "keep"
    selected[0].path.unlink()
    with pytest.raises(OmniError, match="size limit"):
        download_assets(selected, TransferOptions(directory=str(tmp_path), max_filesize=1))
    assert not selected[0].path.exists()


def test_inline_text_receipt_contains_no_source_query_or_plaintext_secret(tmp_path):
    item = media(description="Description")
    item.url = "https://video.example/video.mp4?token=signed-secret"
    item.source_url = item.url
    selected = plans(tmp_path, AssetOptions(write_description=True), item)
    result = download_assets(selected, TransferOptions(directory=str(tmp_path)))[0]
    assert "signed-secret" not in json.dumps(result.media.metadata)
    assert "signed-secret" not in Path(result.path + ".omni.json").read_text()


def test_inline_cancel_between_write_and_publish_cleans_temporary_file(tmp_path):
    selected = plans(tmp_path, AssetOptions(write_description=True), media(description="Description"))
    event = threading.Event()
    with patch("omni_dlp.assets.os.fsync", side_effect=lambda _: event.set()):
        with pytest.raises(OmniError, match="cancelled"):
            download_assets(selected, TransferOptions(directory=str(tmp_path), cancel_event=event))
    assert not selected[0].path.exists()
    assert not list(tmp_path.glob(".omni-asset-*"))


def test_asset_plans_can_be_used_without_the_engine(tmp_path):
    item = Media("https://example.test/page", id="example", kind="document")
    plan = AssetPlan(item, tmp_path / "standalone.description", "standalone", "description")
    result = download_assets([plan], TransferOptions(directory=str(tmp_path)))[0]
    assert Path(result.path).read_text() == "standalone"


def test_subtitle_selection_matches_upstream_for_supported_language_cases(tmp_path):
    upstream = pytest.importorskip("yt_dlp")
    manual = {"fr": [track("fr")], "en-US": [track("en-US")], "ja": [track("ja")], "live_chat": [track("live_chat", "json")]}
    automatic = {"en": [track(automatic=True)], "ja": [track("ja", automatic=True)]}
    for query in (None, "en.*,ja", "all,-live_chat", "-en", "missing", "ja,en.*,-en"):
        options = {"writesubtitles": True, "writeautomaticsub": True, "subtitlesformat": "vtt/srt/best", "quiet": True}
        if query is not None:
            options["subtitleslangs"] = query.split(",")
        with upstream.YoutubeDL(options) as downloader:
            expected = downloader.process_subtitles("fixture", manual, automatic)
        selected = plans(tmp_path, AssetOptions(write_subs=True, write_auto_subs=True, sub_langs=query),
                         media(subtitles=manual, automatic_captions=automatic))
        assert [plan.language for plan in selected] == list(expected or {})
        assert [plan.media.url for plan in selected] == [entry["url"] for entry in (expected or {}).values()]


def test_large_regex_selection_has_explicit_work_bound(tmp_path):
    subtitles = {"e" * 110 + str(index): [track()] for index in range(200)}
    query = ",".join(["e*" * 60 + "z"] * 8)
    with pytest.raises(OmniError, match="work limit"):
        plans(tmp_path, AssetOptions(write_subs=True, sub_langs=query), media(subtitles=subtitles))


def test_relative_and_resolved_root_aliases_produce_same_safe_output(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    selected = plans(alias, AssetOptions(write_description=True), media(description="text"))
    result = download_assets(selected, TransferOptions(directory=str(alias)))[0]
    assert Path(result.path) == actual / "video.description"
    assert Path(result.path).read_text() == "text"


def test_empty_description_is_valid_and_missing_description_is_omitted(tmp_path):
    selected = plans(tmp_path, AssetOptions(write_description=True), media(description=""))
    result = download_assets(selected, TransferOptions(directory=str(tmp_path)))[0]
    assert result.size == 0 and Path(result.path).read_bytes() == b""
    assert plans(tmp_path, AssetOptions(write_description=True), media()) == []


def test_manual_and_automatic_captions_on_same_endpoint_have_distinct_receipts(tmp_path):
    item = media(subtitles={"en": [{"url": "https://captions.example/timedtext?lang=en", "ext": "vtt"}]},
                 automatic_captions={"en": [{"url": "https://captions.example/timedtext?lang=en&kind=asr", "ext": "vtt"}]})
    manual = plans(tmp_path, AssetOptions(write_subs=True), item)[0]
    automatic = plans(tmp_path, AssetOptions(write_auto_subs=True), item)[0]
    assert manual.path == automatic.path
    assert manual.media.metadata["resource_id"] != automatic.media.metadata["resource_id"]


def test_query_parameters_identify_distinct_assets_without_leaking_tokens(tmp_path):
    first = media(thumbnails=[{"url": "https://images.example/get?id=first&token=private-token"}])
    second = media(thumbnails=[{"url": "https://images.example/get?id=second&token=private-token"}])
    left = plans(tmp_path, AssetOptions(write_thumbnail=True), first)[0]
    right = plans(tmp_path, AssetOptions(write_thumbnail=True), second)[0]
    assert left.path == right.path
    assert left.media.metadata["resource_id"] != right.media.metadata["resource_id"]
    assert "private-token" not in json.dumps(left.media.metadata)
