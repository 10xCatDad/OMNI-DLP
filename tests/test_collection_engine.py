"""Offline collection/discovery integration, including incomplete source evidence."""
from dataclasses import replace
import json
from unittest.mock import patch

import pytest

from omni_dlp.collection_selection import CollectionOptions
from omni_dlp.engine import Downloader, Options
from omni_dlp.models import Media, OmniError


def entries(count=4):
    return [Media(f"https://media.example/{index}.mp3", id=str(index), kind="audio", ext="mp3")
            for index in range(1, count + 1)]


def inspect(source, **options):
    downloader = Downloader(Options(metadata_only=True, **options))
    with patch("omni_dlp.engine.discover_plugin", return_value=source):
        return downloader.inspect("https://media.example/collection")


def test_collection_disabled_preserves_existing_filter_limit_and_metadata():
    source = entries()
    source[0] = replace(source[0], kind="image", ext="png")
    assert inspect(source, media_types={"audio"}, max_items=2) == source[1:3]
    assert all(item.metadata == {} for item in source)


@pytest.mark.parametrize("at", [0, 2])
def test_one_reported_count_is_propagated_before_bounds(at):
    source = entries()
    source[at].metadata["playlist_count"] = 8
    result = inspect(source, max_items=2, collection=CollectionOptions(playlist_items="-8:-7"))
    assert [item.id for item in result] == ["1", "2"]
    assert all(item.metadata["playlist_count"] == 8 for item in result)
    assert all(item.metadata["collection_truncated"] is True for item in result)
    assert source[1].metadata == {}
    with pytest.raises(OmniError, match="not enumerated"):
        inspect(source, max_items=2, collection=CollectionOptions(playlist_items="-1"))


@pytest.mark.parametrize("metadata", [{"collection_complete": False}, {"collection_truncated": True},
    {"collection_skipped": 2}, {"discovery_limited": True}])
def test_one_incomplete_marker_keeps_total_unknown_for_every_entry(metadata):
    source = entries()
    source[-1].metadata.update(metadata)
    result = inspect(source, collection=CollectionOptions(playlist_items="1:2"))
    assert len(result) == 2
    assert all(item.metadata.get("playlist_count") is None for item in result)
    assert all(item.metadata["collection_complete"] is False for item in result)
    with pytest.raises(OmniError, match="count|total"):
        inspect(source, collection=CollectionOptions(playlist_items="-1"))


def test_exact_limit_without_completion_evidence_remains_unknown():
    source = entries()
    result = inspect(source, max_items=4, collection=CollectionOptions(playlist_items="1"))
    assert result[0].metadata.get("playlist_count") is None
    assert result[0].metadata["discovery_limited"] is True
    with pytest.raises(OmniError, match="count|total"):
        inspect(source, max_items=4, collection=CollectionOptions(playlist_items="-1"))


@pytest.mark.parametrize("metadata", [{"collection_complete": True}, {"playlist_count": 4}])
def test_exact_limit_with_source_completion_evidence_can_resolve_last(metadata):
    source = entries()
    source[-1].metadata.update(metadata)
    result = inspect(source, max_items=4, collection=CollectionOptions(playlist_items="-1"))
    assert [item.id for item in result] == ["4"]
    assert result[0].metadata["playlist_count"] == 4
    assert result[0].metadata["collection_complete"] is True


def test_incomplete_evidence_wins_over_complete_flag_on_another_entry():
    source = entries()
    source[0].metadata["collection_complete"] = True
    source[-1].metadata["collection_complete"] = False
    with pytest.raises(OmniError, match="count|total"):
        inspect(source, max_items=4, collection=CollectionOptions(playlist_items="-1"))


def test_unreported_source_gaps_do_not_invent_a_count():
    source = entries(2)
    source[0].metadata["playlist_index"] = 5
    source[1].metadata["playlist_index"] = 6
    result = inspect(source, collection=CollectionOptions(playlist_items="5"))
    assert [item.id for item in result] == ["1"]
    assert result[0].metadata.get("playlist_count") is None
    with pytest.raises(OmniError, match="count|total"):
        inspect(source, collection=CollectionOptions(playlist_items="-1"))


def test_kind_filter_and_local_bounds_preserve_original_positions():
    source = entries()
    source[0] = replace(source[0], kind="image", ext="png")
    result = inspect(source, media_types={"audio"}, max_items=2,
                     collection=CollectionOptions(playlist_items="2:3", playlist_reverse=True))
    assert [item.id for item in result] == ["3", "2"]
    assert [item.metadata["playlist_index"] for item in result] == [3, 2]
    assert all(item.metadata["playlist_count"] == 4 for item in result)
    assert all(item.metadata["collection_truncated"] is True for item in result)


def test_kind_filter_does_not_claim_truncation_when_nothing_exceeds_limit():
    source = entries()
    source[0] = replace(source[0], kind="image", ext="png")
    result = inspect(source, media_types={"image"}, max_items=2, collection=CollectionOptions())
    assert [item.id for item in result] == ["1"]
    assert result[0].metadata["playlist_count"] == 4
    assert "collection_truncated" not in result[0].metadata


def test_conflicting_counts_are_rejected_even_when_one_entry_would_be_removed():
    source = entries()
    source[0].metadata["playlist_count"] = 4
    source[-1] = replace(source[-1], kind="image", metadata={"playlist_count": 8})
    with pytest.raises(OmniError, match="Conflicting"):
        inspect(source, media_types={"audio"}, max_items=2, collection=CollectionOptions())


def test_selection_happens_before_manifest_expansion():
    downloader = Downloader(Options(collection=CollectionOptions(playlist_items="2")))
    source = entries()
    with patch("omni_dlp.engine.discover_plugin", return_value=source), \
            patch.object(downloader, "_expand_manifests", side_effect=lambda items, _: items) as expand:
        result = downloader.inspect("https://media.example/collection")
    assert [item.id for item in result] == ["2"]
    assert [item.id for item in expand.call_args.args[0]] == ["2"]


def test_direct_urls_apply_collection_match_filter_without_network():
    downloader = Downloader(Options(direct=True, collection=CollectionOptions(match_filter="ext=png")))
    with patch("omni_dlp.engine.discover_plugin") as plugin:
        assert downloader.inspect("https://media.example/song.mp3") == []
        assert len(downloader.inspect("https://media.example/image.png")) == 1
        plugin.assert_not_called()


@pytest.mark.parametrize("collection,expected", [(None, {"audio"}), (CollectionOptions(), None)])
def test_collection_discovery_defers_kind_filter_to_preserve_source_indices(collection, expected):
    downloader = Downloader(Options(media_types={"audio"}, collection=collection, metadata_only=True))
    with patch("omni_dlp.engine.discover_plugin", return_value=entries()) as plugin:
        downloader.inspect("https://media.example/collection")
    assert plugin.call_args.kwargs["media_types"] == expected


def test_native_soundcloud_collection_keeps_requested_artwork_mode():
    url = "https://soundcloud.com/fixture/sets/collection"
    tracks = [{"id": index, "kind": "track", "title": f"Song {index}", "sharing": "public",
               "policy": "ALLOW", "streamable": True, "duration": 1000,
               "permalink_url": f"https://soundcloud.com/fixture/song-{index}",
               "user": {"username": "Fixture"}, "media": {"transcodings": []},
               "artwork_url": f"https://i1.sndcdn.com/artworks-{index}-large.jpg"}
              for index in (1, 2)]
    playlist = {"kind": "playlist", "id": 10, "sharing": "public", "title": "Fixture set",
                "permalink_url": url, "tracks": tracks, "track_count": 2}
    page = ("<script>window.__sc_hydration = " +
            json.dumps([{"hydratable": "playlist", "data": playlist}]) + ";</script>").encode()
    downloader = Downloader(Options(plugins=False, media_types={"image"},
                                    collection=CollectionOptions(playlist_items="2")))
    with patch("omni_dlp.extractors.common.ExtractorContext.get_document", return_value=(page, url, {})), \
            patch("omni_dlp.extractors.common.ExtractorContext.get_json") as playback:
        result = downloader.inspect(url)
    assert len(result) == 1
    assert result[0].kind == "image"
    assert result[0].url == "https://i1.sndcdn.com/artworks-2-large.jpg"
    assert result[0].metadata["playlist_index"] == 2
    assert result[0].metadata["playlist_count"] == 2
    playback.assert_not_called()
