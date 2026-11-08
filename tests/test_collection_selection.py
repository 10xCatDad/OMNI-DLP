"""Collection selection uses source positions and never invents completeness."""
from datetime import date, datetime, timezone
import random
from unittest.mock import patch

import pytest

from omni_dlp import Media, OmniError
from omni_dlp.collection_selection import CollectionOptions, select_entries


def entries(count=8):
    return [Media(f"https://media.example/{index}", title=f"Item {index}", id=str(index),
                  metadata={"duration": index * 30, "view_count": index * 1000})
            for index in range(1, count + 1)]


def ids(result):
    return [entry.id for entry in result]


@pytest.mark.parametrize("spec,expected", [
    ("1:3,7,-5::2", [1, 2, 3, 7, 4, 6, 8]),
    ("::-1", [8, 7, 6, 5, 4, 3, 2, 1]), ("-1", [8]), ("-3:-1", [6, 7, 8]),
    ("8:1:-2", [8, 6, 4, 2]), ("1:5:2", [1, 3, 5]), ("0", []),
    ("0:4", [1, 2, 3, 4]), ("0:4:2", [2, 4]), ("3:0:-1", [3, 2, 1]),
    ("-20:3", [1, 2, 3]), ("3:20", [3, 4, 5, 6, 7, 8]), ("1-3", [1, 2, 3]),
    ("3-1", []), ("-3--1", [6, 7, 8]), ("-3-3", []), ("1:inf:2", [1, 3, 5, 7]),
    ("3:1", []), ("+2", [2]), ("1,1,3,1", [1, 3]), (":", list(range(1, 9))),
    ("4:", [4, 5, 6, 7, 8]), ("-20", []), ("20:1:-3", [8, 5, 2]),
    ("::2", [1, 3, 5, 7]), ("1-8:3", [1, 4, 7]), ("2:infinite:2", [2, 4, 6, 8]),
])
def test_playlist_indices(spec, expected):
    selected = select_entries(entries(), CollectionOptions(playlist_items=spec))
    assert ids(selected) == [str(index) for index in expected]
    assert [entry.metadata["playlist_index"] for entry in selected] == expected
    assert all(entry.metadata["playlist_count"] == 8 for entry in selected)


def test_start_end_are_inclusive_and_items_override_them():
    assert ids(select_entries(entries(), CollectionOptions(playlist_start=3, playlist_end=5))) == ["3", "4", "5"]
    assert ids(select_entries(entries(), CollectionOptions(playlist_start=7, playlist_end=-1))) == ["7", "8"]
    assert ids(select_entries(entries(), CollectionOptions(playlist_items="2,4", playlist_start=6, playlist_end=7))) == ["2", "4"]
    assert select_entries(entries(), CollectionOptions(playlist_end=0)) == []


def test_reverse_random_and_source_metadata_are_preserved():
    original = entries()
    state = random.getstate()
    selected = select_entries(original, CollectionOptions(playlist_items="2:6", playlist_random=True, random_seed=123))
    expected = list(range(2, 7))
    random.Random(123).shuffle(expected)
    assert ids(selected) == [str(index) for index in expected]
    assert [entry.metadata["playlist_index"] for entry in selected] == expected
    assert all(entry.metadata["playlist_count"] == 8 for entry in selected)
    assert random.getstate() == state
    assert all("playlist_index" not in entry.metadata for entry in original)
    assert ids(select_entries(original, CollectionOptions(playlist_reverse=True, playlist_random=True, random_seed=123))) == list("87654321")


def test_positive_indices_use_source_positions_after_extractor_filtering():
    source = [entries()[index - 1] for index in (2, 5, 8)]
    for media, index in zip(source, (2, 5, 8)):
        media.metadata.update(playlist_index=index, playlist_count=8)
    selected = select_entries(source, CollectionOptions(playlist_items="2:8:3"))
    assert ids(selected) == ["2", "5", "8"]
    assert all(entry.metadata["playlist_count"] == 8 and not entry.metadata["collection_complete"] for entry in selected)
    assert ids(select_entries(source, CollectionOptions(playlist_items="-1"))) == ["8"]
    with pytest.raises(OmniError, match="not enumerated"):
        select_entries(source, CollectionOptions(playlist_items="-3:-1"))


def test_unknown_truncated_count_is_not_inferred_from_loaded_prefix():
    source = entries(3)
    for entry in source:
        entry.metadata["collection_truncated"] = True
    selected = select_entries(source)
    assert all("playlist_count" not in entry.metadata for entry in selected)
    assert all(not entry.metadata["collection_complete"] for entry in selected)
    assert ids(select_entries(source, CollectionOptions(playlist_items="1:2"))) == ["1", "2"]
    for expression in ("-1", "-2:", "::-1", "1:-1"):
        with pytest.raises(OmniError, match="known source playlist_count"):
            select_entries(source, CollectionOptions(playlist_items=expression))


def test_known_truncated_count_supports_only_available_end_relative_positions():
    source = entries(3)
    for entry in source:
        entry.metadata.update(playlist_count=8, discovery_limited=True)
    assert ids(select_entries(source, CollectionOptions(playlist_items="-8:-6"))) == ["1", "2", "3"]
    for expression in ("-1", "-2:", "::-1"):
        with pytest.raises(OmniError, match="not enumerated"):
            select_entries(source, CollectionOptions(playlist_items=expression))


@pytest.mark.parametrize("metadata", [
    [{"playlist_count": 4}, {"playlist_count": 5}],
    [{"playlist_index": 1}, {"playlist_index": 1}],
    [{"playlist_index": 3, "playlist_count": 2}],
    [{"playlist_index": True}], [{"playlist_count": True}], [{"playlist_index": 0}],
    [{"playlist_index": -1}], [{"playlist_count": -1}], [{"playlist_count": "10"}],
])
def test_invalid_collection_metadata_is_actionable(metadata):
    source = entries(len(metadata))
    for entry, values in zip(source, metadata):
        entry.metadata.update(values)
    with pytest.raises(OmniError):
        select_entries(source)


def test_large_bounds_do_not_expand_billions_of_indices():
    assert ids(select_entries(entries(), CollectionOptions(playlist_items="1:1000000000000"))) == list("12345678")
    assert ids(select_entries(entries(), CollectionOptions(playlist_items="1000000000000:1:-3"))) == ["7", "4", "1"]
    assert select_entries([], CollectionOptions(playlist_items="::-1")) == []


@pytest.mark.parametrize("spec", ["", "1,,2", ",1", "1,", "::", "1:2:", "1:2:0", "1:2:3:4", "foo",
    "1..3", "1/2", "1;print(1)", "1:NaN", "1:1e3", "1000000000001", ":+inf", "1" * 8193])
def test_invalid_item_expressions(spec):
    with pytest.raises(OmniError):
        select_entries(entries(), CollectionOptions(playlist_items=spec))


@pytest.mark.parametrize("options", [
    CollectionOptions(playlist_start=0), CollectionOptions(playlist_start=True), CollectionOptions(playlist_start=1.5),
    CollectionOptions(playlist_end=-2), CollectionOptions(playlist_end=True), CollectionOptions(random_seed=True),
    CollectionOptions(playlist_reverse=1), CollectionOptions(playlist_random="yes"),
])
def test_invalid_option_types(options):
    with pytest.raises(OmniError):
        select_entries(entries(), options)


def dated_entries():
    dates = ["20240101", "2024-02-29", "20240331", None]
    return [Media(f"https://media.example/{index}", id=str(index), metadata={"upload_date": value})
            for index, value in enumerate(dates, 1)]


def test_date_ranges_are_inclusive_and_unknown_dates_remain_eligible():
    source = dated_entries()
    assert ids(select_entries(source, CollectionOptions(dateafter="20240229", datebefore="20240331"))) == ["2", "3", "4"]
    assert ids(select_entries(source, CollectionOptions(date="2024-02-29"))) == ["2", "4"]
    assert ids(select_entries(source, CollectionOptions(datebefore="20240101"))) == ["1", "4"]
    assert ids(select_entries(source, CollectionOptions(dateafter="20240331"))) == ["3", "4"]


def test_relative_dates_use_utc_and_calendar_month_clamping():
    with patch("omni_dlp.collection_selection._utc_today", return_value=date(2024, 3, 31)):
        assert ids(select_entries(dated_entries(), CollectionOptions(date="today-1month"))) == ["2", "4"]
        assert ids(select_entries(dated_entries(), CollectionOptions(date="today-31days"))) == ["2", "4"]
        assert ids(select_entries(dated_entries(), CollectionOptions(date="now"))) == ["3", "4"]
        assert ids(select_entries(dated_entries(), CollectionOptions(datebefore="yesterday"))) == ["1", "2", "4"]
    source = [Media("https://media.example/leap", metadata={"upload_date": "20230228"})]
    with patch("omni_dlp.collection_selection._utc_today", return_value=date(2024, 2, 29)):
        assert len(select_entries(source, CollectionOptions(date="today-1year"))) == 1


def test_timestamp_fallback_is_utc():
    item = Media("https://media.example/file", metadata={"timestamp": datetime(2024, 2, 29, 23, 59, tzinfo=timezone.utc).timestamp()})
    assert len(select_entries([item], CollectionOptions(date="20240229"))) == 1
    assert select_entries([item], CollectionOptions(date="20240301")) == []


@pytest.mark.parametrize("options", [CollectionOptions(date="20240230"), CollectionOptions(date="tomorrow"),
    CollectionOptions(date="today+1day"), CollectionOptions(date="today-999999years"),
    CollectionOptions(date="2024/02/29"), CollectionOptions(date=""), CollectionOptions(date=20240229),
    CollectionOptions(date="20240229", dateafter="20240101"),
    CollectionOptions(dateafter="20240301", datebefore="20240229")])
def test_invalid_dates(options):
    with pytest.raises(OmniError):
        select_entries(dated_entries(), options)


@pytest.mark.parametrize("metadata", [{"upload_date": "not-a-date"}, {"upload_date": []}, {"upload_date": "today"},
    {"timestamp": float("nan")}, {"timestamp": True}, {"timestamp": 1e30}])
def test_bad_source_dates_fail_only_when_date_filter_requested(metadata):
    item = Media("https://media.example/file", metadata=metadata)
    assert len(select_entries([item])) == 1
    with pytest.raises(OmniError):
        select_entries([item], CollectionOptions(datebefore="20250101"))


@pytest.mark.parametrize("expression,expected", [
    ("duration>=60 & view_count<5K", [2, 3, 4]), ("duration<2:00", [1, 2, 3]),
    ("duration<2minutes", [1, 2, 3]), ("duration!<60", [2, 3, 4, 5, 6, 7, 8]),
    ("title^='Item 2'", [2]), ("id=7", [7]), ("id!=7", [1, 2, 3, 4, 5, 6, 8]),
    ("view_count<=1K", [1]), ("view_count<1KiB", [1]),
    ("uploader", []), ("!uploader", list(range(1, 9))),
    ("view_count>?5000 & !is_live", [6, 7, 8]), ("missing>?1", list(range(1, 9))),
    ("missing!=?value", list(range(1, 9))), ("id<3", [1, 2]),
])
def test_match_filters(expression, expected):
    assert ids(select_entries(entries(), CollectionOptions(match_filter=expression))) == [str(index) for index in expected]


def test_presence_zero_empty_and_boolean_semantics():
    entry = Media("https://media.example/file", title="", metadata={"duration": 0, "is_live": False, "was_live": True})
    for expression in ("duration", "title", "!is_live", "was_live", "duration=0"):
        assert len(select_entries([entry], CollectionOptions(match_filter=expression))) == 1
    for expression in ("!duration", "!title", "is_live", "!was_live"):
        assert select_entries([entry], CollectionOptions(match_filter=expression)) == []


def test_string_filter_escaping_and_aliases():
    entry = Media("https://media.example/file", title="Cat & Dog's clip", ext="webm", metadata={"age_limit": 18, "average_rating": 4.5, "uploader": "Example"})
    for expression in (r'''title*='Cat \& Dog\'s' ''', '''title*="Cat & Dog"''', "ext$=bm & uploader!^=Other", "age<=18 & rating>=4.5"):
        assert len(select_entries([entry], CollectionOptions(match_filter=expression))) == 1


def test_selection_order_precedes_metadata_filters():
    result = select_entries(entries(), CollectionOptions(playlist_items="1:4", playlist_reverse=True, match_filter="duration>60"))
    assert ids(result) == ["4", "3"]
    assert [entry.metadata["playlist_index"] for entry in result] == [4, 3]
    assert all(entry.metadata["playlist_count"] == 8 for entry in result)


@pytest.mark.parametrize("expression", ["", "&title", "title&", "title&&id", "title~='.*'", "title!~=cat",
    "duration<nan", "duration<inf", "duration*=1", "duration<1e3", "title='unterminated", "-",
    "__import__('os')", "title=a || id=1", "x" * 8193])
def test_unsupported_match_filters_are_explicit(expression):
    with pytest.raises(OmniError):
        select_entries(entries(), CollectionOptions(match_filter=expression))


def test_non_scalar_metadata_comparisons_are_explicit():
    item = Media("https://media.example/file", metadata={"tags": ["a", "b"]})
    with pytest.raises(OmniError, match="scalar"):
        select_entries([item], CollectionOptions(match_filter="tags*=a"))


@pytest.mark.parametrize("spec", ["1:3,7,-5::2", "::-1", "-1", "-3:-1", "8:1:-2", "1:5:2", "0", "0:4",
    "0:4:2", "3:0:-1", "-20:3", "3:20", "1-3", "3-1", "-3--1", "-3-3", "1:inf:2", "3:1",
    "+2", "1,1,3,1", ":", "4:", "-20", "20:1:-3", "::2", "1-8:3", "2:infinite:2"])
def test_differential_playlist_selection(spec):
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.utils import PlaylistEntries
    ydl = yt_dlp.YoutubeDL({"quiet": True, "playlist_items": spec})
    source = [{"id": str(i), "title": str(i)} for i in range(1, 9)]
    selected = PlaylistEntries(ydl, {"entries": source}).get_requested_items()
    expected = list(dict.fromkeys(index for index, _ in selected))
    assert ids(select_entries(entries(), CollectionOptions(playlist_items=spec))) == [str(index) for index in expected]


@pytest.mark.parametrize("expression", ["duration>=60 & view_count<5K", "duration<2:00", "duration<2minutes",
    "duration!<60", "title^='Item 2'", "id=7", "id!=7", "view_count<=1K", "view_count<1KiB", "uploader",
    "!uploader", "view_count>?5000 & !is_live", "missing>?1", "missing!=?value", "id<3", "duration", "!duration"])
def test_differential_match_filters(expression):
    pytest.importorskip("yt_dlp")
    from yt_dlp.utils import match_filter_func
    predicate = match_filter_func(expression)
    source = entries()
    expected = [entry.id for entry in source if predicate({**entry.to_dict(), **entry.metadata}) is None]
    assert ids(select_entries(source, CollectionOptions(match_filter=expression))) == expected


@pytest.mark.parametrize("first,last", [("20240101", "20240331"), ("20240229", "20240229"),
    (None, "20240101"), ("20240331", None), ("today-1month", "today")])
def test_differential_date_ranges(first, last):
    pytest.importorskip("yt_dlp")
    from yt_dlp.utils import DateRange
    upstream = DateRange(first, last)
    source = dated_entries()
    expected = [entry.id for entry in source if entry.metadata["upload_date"] is None
                or entry.metadata["upload_date"].replace("-", "") in upstream]
    assert ids(select_entries(source, CollectionOptions(dateafter=first, datebefore=last))) == expected


def test_seeded_playlist_differential_matrix():
    yt_dlp = pytest.importorskip("yt_dlp")
    from yt_dlp.utils import PlaylistEntries
    randomizer = random.Random(918)
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    for _ in range(600):
        count = randomizer.randrange(0, 21)
        segments = []
        for _ in range(randomizer.randrange(1, 5)):
            start = randomizer.choice([None, *range(-30, 31)])
            end = randomizer.choice([None, *range(-30, 31)])
            step = randomizer.choice([-5, -3, -2, -1, 1, 2, 3, 5])
            segments.append(f'{start if start is not None else ""}:{end if end is not None else ""}:{step}')
        spec = ",".join(segments)
        ydl.params["playlist_items"] = spec
        source = [{"id": str(index)} for index in range(1, count + 1)]
        chosen = PlaylistEntries(ydl, {"entries": source}).get_requested_items()
        expected = list(dict.fromkeys(index for index, _ in chosen))
        actual = [int(entry.id) for entry in select_entries(entries(count), CollectionOptions(playlist_items=spec))]
        assert actual == expected, (count, spec)


def test_oversized_numeric_literals_and_timestamps_are_actionable():
    with pytest.raises(OmniError):
        select_entries(entries(), CollectionOptions(match_filter="duration<" + "9" * 5000))
    huge = Media("https://media.example/file", metadata={"timestamp": 10**500})
    with pytest.raises(OmniError):
        select_entries([huge], CollectionOptions(date="20240101"))
    # Long string IDs remain literal comparisons rather than numeric coercion.
    identifier = "9" * 5000
    item = Media("https://media.example/file", id=identifier)
    assert len(select_entries([item], CollectionOptions(match_filter="id=" + identifier))) == 1


_MATCH_SIZES = [("1K", 1000), ("1k", 1024), ("1KB", 1000), ("1kB", 1024),
                ("1kb", 1000), ("1Kb", 1000), ("1Ki", 1024), ("1KiB", 1024),
                ("1M", 1000000), ("1m", 1048576), ("1MB", 1000000), ("1mB", 1048576),
                ("1mb", 1000000), ("1MiB", 1048576), ("1G", 1000000000), ("1GiB", 1073741824),
                ("1.5K", 1500), ("1.5k", 1536), ("1.5KiB", 1536), ("1.001KiB", 1025),
                ("0.0009K", 1), ("0.0005K", 0), ("0.0015K", 2)]


@pytest.mark.parametrize("literal,threshold", _MATCH_SIZES)
def test_match_size_unit_boundaries(literal, threshold):
    source = [Media(f"https://media.example/{n}", id=str(n), metadata={"filesize": n})
              for n in range(max(0, threshold - 1), threshold + 2)]
    assert ids(select_entries(source, CollectionOptions(match_filter=f"filesize={literal}"))) == [str(threshold)]


@pytest.mark.parametrize("literal,threshold", _MATCH_SIZES)
def test_differential_match_size_boundaries(literal, threshold):
    pytest.importorskip("yt_dlp")
    from yt_dlp.utils import match_filter_func
    source = [Media(f"https://media.example/{n}", id=str(n), metadata={"filesize": n})
              for n in range(max(0, threshold - 1), threshold + 2)]
    source.append(Media("https://media.example/unknown", id="unknown"))
    for symbol in ("=", "!=", "<", "<=", ">", ">="):
        for unknown in ("", "?"):
            expression = f"filesize{symbol}{unknown}{literal}"
            predicate = match_filter_func(expression)
            expected = [item.id for item in source if predicate({**item.to_dict(), **item.metadata}) is None]
            assert ids(select_entries(source, CollectionOptions(match_filter=expression))) == expected


@pytest.mark.parametrize("literal", ["1ki", "1kiB", "1KIB", "1Mib", "1KBb"])
def test_malformed_size_units_are_explicit(literal):
    with pytest.raises(OmniError, match="Numeric"):
        select_entries(entries(), CollectionOptions(match_filter=f"filesize<{literal}"))


def test_native_bare_decimal_match_preserves_fraction():
    source = [Media(f"https://media.example/{n}", id=str(n), metadata={"rating": n}) for n in (4, 4.5, 5)]
    assert ids(select_entries(source, CollectionOptions(match_filter="rating=4.5"))) == ["4.5"]
