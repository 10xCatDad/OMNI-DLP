"""Selector semantics, hostile input, and optional upstream differential fixtures."""
from dataclasses import replace
import math
import operator

import pytest

from omni_dlp.models import Format, OmniError
from omni_dlp.selection import select_formats, sort_formats


def ids(groups):
    return [[fmt.format_id for fmt in group] for group in groups]


def format_(id, *, height=None, audio="none", video="avc1", ext="mp4", **kwargs):
    return Format(f"https://media.example/{id}", id, ext, acodec=audio, vcodec=video,
                  height=height, width=height * 16 // 9 if height else None, **kwargs)


@pytest.fixture
def formats():
    return [
        format_("a-low", audio="mp4a", video="none", ext="m4a", abr=64),
        format_("a-high", audio="mp4a", video="none", ext="m4a", abr=128),
        format_("v-low", height=360, filesize=5 * 1024**2, tbr=500),
        format_("v-high", height=1080, filesize=20 * 1024**2, tbr=2000),
        format_("18", height=360, audio="mp4a", filesize=6 * 1024**2, tbr=600),
        format_("22", height=720, audio="mp4a", filesize=12 * 1024**2, tbr=1200),
    ]


@pytest.mark.parametrize("expression,expected", [
    ("best", [["22"]]), ("b", [["22"]]), ("worst", [["18"]]),
    ("bv", [["v-high"]]), ("wv", [["v-low"]]), ("ba", [["a-high"]]),
    ("wa", [["a-low"]]), ("bv*", [["v-high"]]), ("ba*", [["22"]]),
    ("bv+ba/b", [["v-high", "a-high"]]), ("(bv,ba)", [["v-high"], ["a-high"]]),
    ("missing/18", [["18"]]), ("missing,18", [["18"]]),
    ("bv.2", [["v-low"]]), ("bv.3", []), ("ba.2", [["a-low"]]),
    ("mp4", [["22"]]), ("m4a", [["a-high"]]),
    ("bv[height<=720]+ba", [["v-low", "a-high"]]),
    ("(bv+ba/b)[height<=?720]", [["v-low", "a-high"]]),
    ("(bv+ba)[height<=720]", []),
    ("[height<=720]", [["22"]]),
    ("b[filesize<10M]", [["18"]]),
    ("b[format_id!=22]", [["18"]]),
    ("bv[format_id^=v-][format_id$=high]", [["v-high"]]),
    ("ba[format_id!*='low']", [["a-high"]]),
    ("best+ba", [["22"]]), ("bv+best", [["v-high"]]),
    ("ba+bv", [["a-high", "v-high"]]), ("bv+ba+ba", [["v-high", "a-high"]]),
])
def test_selection_semantics(formats, expression, expected):
    assert ids(select_formats(formats, expression)) == expected


def test_missing_is_unknown_not_zero(formats):
    assert ids(select_formats(formats, "ba[height<=720]")) == []
    assert ids(select_formats(formats, "ba[height<=?720]")) == [["a-high"]]
    assert ids(select_formats(formats, "ba[height!=?0]")) == [["a-high"]]
    assert ids(select_formats(formats, "ba[height!=0]")) == []
    zero = replace(formats[0], height=0)
    assert ids(select_formats([zero], "ba[height=0]")) == [["a-low"]]


def test_string_filters_and_quoting():
    audio = format_("audio", video="none", audio="aac", language="en-US",
                    metadata={"format_note": "a ] quoted + / , note"})
    assert ids(select_formats([audio], 'ba[format_note="a ] quoted + / , note"]')) == [["audio"]]
    assert ids(select_formats([audio], 'ba[language^="en"][language$="US"][language!*=fr]')) == [["audio"]]
    assert ids(select_formats([audio], 'ba[language!^="fr"]')) == [["audio"]]
    assert ids(select_formats([audio], 'ba[language!$="GB"]')) == [["audio"]]


def test_explicit_absence_vs_unknown_codecs():
    unknown = format_("unknown", video="", audio="")
    silent = format_("silent", audio="none", video="avc1", height=720)
    assert ids(select_formats([unknown], "best")) == [["unknown"]]
    assert ids(select_formats([silent], "best")) == [["silent"]]
    assert ids(select_formats([silent], "ba")) == []


def test_non_av_formats_are_first_class():
    pictures = [format_("small", video="none", audio="none", ext="jpg", height=360),
                format_("large", video="none", audio="none", ext="jpg", height=1080)]
    assert ids(select_formats(pictures, "best")) == [["large"]]
    assert ids(select_formats(pictures, "worst")) == [["small"]]
    assert ids(select_formats(pictures, "jpg")) == [["large"]]
    assert ids(select_formats(pictures, "bv")) == []


def test_audio_only_best_fallback(formats):
    audio = [fmt for fmt in formats if fmt.vcodec == "none"]
    assert ids(select_formats(audio, "best")) == [["a-high"]]
    assert ids(select_formats(audio)) == [["a-high"]]


def test_video_extension_separate_fallback(formats):
    separate = [fmt for fmt in formats if fmt.acodec == "none" or fmt.vcodec == "none"]
    assert ids(select_formats(separate, "mp4")) == [["v-high"]]


def test_multiple_and_nested_operators(formats):
    assert ids(select_formats(formats, "(missing/bv)+(missing/ba),worst")) == [["v-high", "a-high"], ["18"]]
    assert ids(select_formats(formats, "(v-low,v-high)+(a-low,a-high)")) == [
        ["v-low", "a-low"], ["v-low", "a-high"], ["v-high", "a-low"], ["v-high", "a-high"]]
    assert ids(select_formats(formats, "all")) == [[f.format_id] for f in reversed(sort_formats(formats))]


def test_hls_merge_respects_audio_groups():
    video = format_("video", height=1080, metadata={"audio_group": "group-a"})
    compatible = format_("compatible", audio="aac", video="none", abr=128, metadata={"audio_group": "group-a"})
    other = format_("other", audio="aac", video="none", abr=256, metadata={"audio_group": "group-b"})
    assert ids(select_formats([video, compatible, other], "bv+ba")) == [["video", "compatible"]]
    assert ids(select_formats([video, compatible, other], "video+other")) == []
    assert ids(select_formats([video, compatible, other], "compatible+bv")) == [["compatible", "video"]]


@pytest.mark.parametrize("sort,expected", [
    ("height", "v-high"), ("+height", "18"), ("height:720", "22"),
    ("height:480", "18"), ("height:100", "18"), ("+height:480", "22"),
    ("height~900", "22"), ("filesize~11M", "22"), ("filesize:10M", "18"),
    ("+filesize", "v-low"),
])
def test_custom_sort_limits(formats, sort, expected):
    assert ids(select_formats(formats, "bv*", sort)) == [[expected]]


def test_sort_metadata_and_codec_aliases():
    a = format_("avc", video="avc1.640028", height=1080, audio="aac", fps=30,
                metadata={"audio_channels": 2, "dynamic_range": "SDR"})
    b = format_("av1", video="av01.0.08M.08", height=1080, audio="opus", fps=60,
                metadata={"audio_channels": 6, "dynamic_range": "DV"})
    assert ids(select_formats([a, b], "best", "vcodec:avc")) == [["avc"]]
    assert ids(select_formats([a, b], "best", "+fps")) == [["avc"]]
    assert ids(select_formats([a, b], "best", "channels")) == [["av1"]]
    assert ids(select_formats([a, b], "best", "hdr:12")) == [["avc"]]
    assert ids(select_formats([a, b], "best", "codec:avc:aac")) == [["avc"]]


def test_unknown_and_nonfinite_sort_values_are_last(formats):
    unknown = replace(formats[-1], format_id="missing", height=None, width=None, filesize=None, tbr=math.nan)
    assert ids(select_formats([unknown, formats[-1]], "best", "+height")) == [["22"]]
    assert ids(select_formats([unknown], "best[height<=720]")) == []


def test_sort_does_not_mutate_input(formats):
    original = formats.copy()
    sort_formats(formats)
    assert formats == original


@pytest.mark.parametrize("expression", [
    "", " ", "+bv", "bv+", "best/", ",best", "best,,worst", "()", "(best", "best)",
    "best[height", "best[height]", "best[height~=720]", "best[height<nan]", "best[height<inf]",
    "best[height*=72]", "best[language>en]", "best[unexpected=1]", "best[ext='mp4]",
    "best[ext=mp 4]", "best[format_note='a'b']", "best|worst", "best;os.system('bad')",
    "mergeall", "a" * 8193, "(" * 49 + "best" + ")" * 49,
])
def test_invalid_expressions_fail_explicitly(expression, formats):
    with pytest.raises(OmniError):
        select_formats(formats, expression)


@pytest.mark.parametrize("sort", ["unknown", "height,,fps", "height,", "-height", "height:nan",
    "height:inf", "height~", "vcodec:imaginary", "ext:invalid", "id:3", "height:1:2", "hdr:unrecognized"])
def test_unsupported_sort_is_explicit(sort, formats):
    with pytest.raises(OmniError):
        select_formats(formats, "best", sort)


def test_output_expansion_limit():
    formats = [format_(str(n), height=720) for n in range(65)]
    with pytest.raises(OmniError, match="too many outputs"):
        select_formats(formats, "all+all")


@pytest.mark.parametrize("expression", [
    "best", "worst", "bv", "ba", "bv*", "ba*", "bv+ba/b", "bv,ba", "missing/18", "mp4", "m4a",
    "bv.2", "ba.2", "b[height<=720]", "bv[height<=720]+ba", "(bv+ba)[height<=?720]",
    "b[filesize<10M]", "bv[format_id^=v-]", "best+ba", "bv+best", "ba+bv", "all",
])
def test_differential_selection_against_upstream(formats, expression):
    yt_dlp = pytest.importorskip("yt_dlp")
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True})
    upstream = [fmt.to_dict() for fmt in formats]
    for record in upstream:
        record.update(record.pop("metadata"))
        record.pop("headers", None)
    info = {"formats": upstream}
    ydl.sort_formats(info)
    chosen = ydl._select_formats(info["formats"], ydl.build_format_selector(expression))
    expected = [[f["format_id"] for f in record.get("requested_formats", [record])] for record in chosen]
    assert ids(select_formats(formats, expression)) == expected


@pytest.mark.parametrize("sort", ["height", "+height", "height:720", "height:480", "+height:480",
    "height~900", "filesize~11M", "filesize:10M", "+filesize", "fps,height", "vcodec:avc", "ext"])
def test_differential_sort_against_upstream(formats, sort):
    yt_dlp = pytest.importorskip("yt_dlp")
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format_sort": sort.split(",")})
    upstream = [fmt.to_dict() for fmt in formats]
    for record in upstream:
        record.update(record.pop("metadata"))
        record.pop("headers", None)
    info = {"formats": upstream}
    ydl.sort_formats(info)
    chosen = ydl._select_formats(info["formats"], ydl.build_format_selector("bv*"))
    assert ids(select_formats(formats, "bv*", sort)) == [[f["format_id"]] for f in chosen]


def test_audio_groups_are_scoped_to_manifest():
    video = format_("video", height=1080, metadata={"audio_group": "group-a", "manifest_url": "https://example/a.m3u8"})
    audio = format_("audio", audio="aac", video="none", abr=128,
                    metadata={"audio_group": "group-b", "manifest_url": "https://example/b.m3u8"})
    assert ids(select_formats([video, audio], "bv+ba")) == [["video", "audio"]]


def test_non_av_extension_registry():
    formats = [format_("archive", video="none", audio="none", ext="zip"),
               format_("font", video="none", audio="none", ext="woff2")]
    assert ids(select_formats(formats, "zip")) == [["archive"]]
    assert ids(select_formats(formats, "woff2")) == [["font"]]


def test_seeded_differential_matrix():
    """Exercise ranking interactions rather than only isolated parser cases."""
    import random
    yt_dlp = pytest.importorskip("yt_dlp")
    randomizer = random.Random(7)
    expressions = ["best", "worst", "bv", "ba", "bv*", "ba*", "bestvideo+bestaudio/best", "all"]
    sorts = [None, "res:720", "+res", "vcodec", "acodec", "ext", "filesize~10M", "+codec:avc:mp4a", "proto"]
    for case in range(12):
        formats = []
        for index in range(12):
            kind = randomizer.choice(["a", "v", "b"])
            height = randomizer.choice([240, 360, 480, 720, 1080, 2160]) if kind != "a" else None
            formats.append(Format(
                f"https://media.example/{index}", str(index),
                randomizer.choice(["mp4", "webm"]) if kind != "a" else randomizer.choice(["m4a", "opus"]),
                vcodec=randomizer.choice(["avc1.64001f", "vp9", "av01"]) if kind != "a" else "none",
                acodec=randomizer.choice(["mp4a.40.2", "opus"]) if kind != "v" else "none",
                height=height, width=round(height * 16 / 9) if height else None,
                abr=randomizer.choice([64, 128, 256]) if kind != "v" else None,
                tbr=randomizer.randrange(100, 3000), fps=randomizer.choice([24, 30, 60]) if height else None,
                filesize=randomizer.randrange(1000, 20000) * 1024,
            ))
        for sort in sorts:
            ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format_sort": sort.split(",") if sort else []})
            info = {"formats": [fmt.to_dict() for fmt in formats]}
            ydl.sort_formats(info)
            for expression in expressions:
                chosen = ydl._select_formats(info["formats"], ydl.build_format_selector(expression))
                expected = [[f["format_id"] for f in item.get("requested_formats", [item])] for item in chosen]
                assert ids(select_formats(formats, expression, sort)) == expected, (case, sort, expression)


_FILTER_SIZES = [("1K", 1000), ("1k", 1024), ("1KB", 1000), ("1kB", 1024),
                 ("1kb", 1000), ("1Kb", 1000), ("1Ki", 1024), ("1KiB", 1024),
                 ("1M", 1000000), ("1m", 1048576), ("1MB", 1000000), ("1mB", 1048576),
                 ("1mb", 1000000), ("1MiB", 1048576), ("1G", 1000000000), ("1GiB", 1073741824),
                 ("1.5K", 1500), ("1.5k", 1536), ("1.5KiB", 1536), ("1.001KiB", 1025),
                 ("0.0009K", 1), ("0.0005K", 0), ("0.0015K", 2)]
_COMPARISONS = {"=": operator.eq, "!=": operator.ne, "<": operator.lt,
                "<=": operator.le, ">": operator.gt, ">=": operator.ge}


@pytest.mark.parametrize("literal,threshold", _FILTER_SIZES)
def test_filter_size_unit_boundaries(literal, threshold):
    formats = [format_(str(n), audio="aac", filesize=n) for n in range(max(0, threshold - 1), threshold + 2)]
    for symbol, compare in _COMPARISONS.items():
        expected = {fmt.format_id for fmt in formats if compare(fmt.filesize, threshold)}
        chosen = select_formats(formats, f"all[filesize{symbol}{literal}]")
        assert {group[0].format_id for group in chosen} == expected


@pytest.mark.parametrize("literal,threshold", _FILTER_SIZES)
def test_differential_filter_size_boundaries(literal, threshold):
    yt_dlp = pytest.importorskip("yt_dlp")
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True})
    formats = [format_(str(n), audio="aac", filesize=n) for n in range(max(0, threshold - 1), threshold + 2)]
    formats.append(format_("unknown", audio="aac"))
    for symbol in _COMPARISONS:
        for unknown in ("", "?"):
            expression = f"filesize{symbol}{unknown}{literal}"
            predicate = ydl._build_format_filter(expression)
            expected = {fmt.format_id for fmt in formats if predicate(fmt.to_dict())}
            chosen = select_formats(formats, f"all[{expression}]")
            assert {group[0].format_id for group in chosen} == expected


@pytest.mark.parametrize("literal", ["1ki", "1kiB", "1KIB", "1Mib", "1KBb"])
def test_malformed_size_units_are_explicit(literal, formats):
    with pytest.raises(OmniError, match="numeric"):
        select_formats(formats, f"all[filesize<{literal}]")


@pytest.mark.parametrize("literal,threshold", [("1K", 1024), ("1k", 1024), ("1M", 1048576),
                                               ("1m", 1048576), ("1.5K", 1536), ("1.001K", 1025)])
@pytest.mark.parametrize("delimiter", [":", "~"])
def test_differential_size_sort_uses_binary_bare_units(literal, threshold, delimiter):
    formats = [format_(str(n), audio="aac", filesize=n) for n in (threshold - 1, threshold, threshold + 1)]
    sort = f"filesize{delimiter}{literal}"
    assert ids(select_formats(formats, "best", sort)) == [[str(threshold)]]
    yt_dlp = pytest.importorskip("yt_dlp")
    ydl = yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "format_sort": [sort]})
    info = {"formats": [fmt.to_dict() for fmt in formats]}
    ydl.sort_formats(info)
    chosen = ydl._select_formats(info["formats"], ydl.build_format_selector("best"))
    assert ids(select_formats(formats, "best", sort)) == [[fmt["format_id"]] for fmt in chosen]


def test_explicit_sort_size_units_are_native_extension():
    formats = [format_(str(n), audio="aac", filesize=n) for n in (999, 1000, 1001, 1023, 1024, 1025)]
    assert ids(select_formats(formats, "best", "filesize:1KB")) == [["1000"]]
    assert ids(select_formats(formats, "best", "filesize:1KiB")) == [["1024"]]
    assert ids(select_formats(formats, "best", "filesize:1kB")) == [["1024"]]
