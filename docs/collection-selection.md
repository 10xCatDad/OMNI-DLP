# Native collection selection

Collection selection applies to the logical entries returned by one source,
before expanding stream formats or manifest variants. It preserves source
positions through filtering, reverse order, and random order. It does not import
or execute yt-dlp.

```python
from omni_dlp.collection_selection import CollectionOptions, select_entries

selected = select_entries(entries, CollectionOptions(
    playlist_items="1:3,7,-5::2",
    dateafter="20240101",
    match_filter="duration>=60 & !is_live",
))
```

`select_entries` returns `Media` copies with copied metadata dictionaries. The
input list and original metadata are not modified. Valid selections with no
matches return an empty list. Invalid syntax, contradictory source metadata,
and ambiguous end-relative requests raise `OmniError`.

## Positions and ordering

| Option or expression | Meaning |
| --- | --- |
| `playlist_items="3"` | Source position 3 |
| `"1,4,7"` | Those positions, in requested order |
| `"2:5"` or `"2-5"` | Inclusive positions 2 through 5 |
| `"1:8:2"` | Positions 1, 3, 5, 7 |
| `"4:"` | Enumerated positions from 4 onward |
| `"-1"` | Last source position, when the total is known |
| `"-3:-1"` | Last three source positions |
| `"::-1"` | Reverse the full source order, when resolvable |
| `"8:1:-2"` | Positions 8, 6, 4, 2 |
| `"1:inf:2"` | Every other enumerated position, starting at 1 |
| `playlist_start=3, playlist_end=5` | Inclusive range when `playlist_items` is omitted |
| `playlist_reverse=True` | Reverse the selected sequence |
| `playlist_random=True, random_seed=123` | Shuffle the selected sequence reproducibly |

Indices are one-based and range endpoints are inclusive. Zero is not an entry:
`0` selects nothing; `0:4:2` selects 2 and 4. Out-of-bounds positions are omitted
for complete collections. A range keeps the step phase of its original start,
so `20:1:-3` against an eight-item collection yields 8, 5, 2.

Repeated positions are included once, in first-requested order. `playlist_items`
takes precedence over `playlist_start` and `playlist_end`. An end of `-1` means
unbounded for the legacy `playlist_end` option; in `playlist_items`, `-1` means
the last position. Reverse ordering takes precedence if both reverse and random
are enabled, matching upstream behavior. Random ordering uses its own generator
and does not change Python's global random state.

The processing order is: select source positions, remove repeated positions,
reverse or shuffle, apply date and metadata filters. Source `playlist_index` and
`playlist_count` remain unchanged by those operations. They do not become the
rank or size of the filtered output.

## Incomplete discovery

Existing source indices are authoritative. When absent, sequential indices are
assigned to the supplied source entries. Existing counts are retained and must
agree. Duplicate indices, non-integer metadata, or indices beyond the declared
count are rejected instead of being silently renumbered.

The selector recognizes `collection_complete=False`, `collection_truncated`,
`collection_skipped`, and `discovery_limited`, as well as gaps in source indices
or a declared count greater than the loaded entries. A truncated list without a
reliable count remains unknown; the number currently loaded is not substituted
for the full count. The `collection_complete` flag describes source enumeration,
not whether every selected entry was downloaded.

Positive selection can operate on the enumerated positions of an incomplete
collection. End-relative selection requires a trustworthy `playlist_count` and
all requested positions to be present. For example, a loaded prefix 1 through 3
with a declared total of 8 can resolve `-8:-6`; `-1` fails because position 8 was
not enumerated. Without a declared total, either request fails. Increasing the
discovery limit or completing pagination can supply the missing evidence.

This module cannot detect an upstream collection changing between API pages or
correct an erroneous source count. Extractors must continue to report pagination
and filtering limitations accurately.

When collection options are active, the downloader assigns source positions
before applying its media-kind filter and local item limit. Counts and
incompleteness are normalized across the whole discovery result, including
entries subsequently removed. A result exactly equal to the discovery limit is
conservatively treated as potentially incomplete unless the source supplies a
count or explicitly reports complete enumeration. Increasing the limit can
resolve this ambiguity. Existing discovery behavior is unchanged when no
collection options are requested.

The current selector requires one record per source position. Multiple returned
assets with the same `playlist_index`, such as GIF/MP4 gallery variants or mixed
audio/artwork records, are rejected when collection selection is active. This
does not prevent ordinary downloading without collection options. Media-kind
filtering by the engine occurs after source validation. Built-in extractors can
use requested media kinds to choose their asset modes earlier; any duplicate
source positions remaining in their returned records still raise an error.

## Dates

`date`, `datebefore`, and `dateafter` accept `YYYYMMDD`, ISO `YYYY-MM-DD`, and
`now`, `today`, or `yesterday`, optionally followed by `-Ndays`, `-Nweeks`,
`-Nmonths`, or `-Nyears`. Singular units also work. Relative dates are anchored
once per selection in UTC. Month/year subtraction uses calendar arithmetic and
clamps the day to the target month's last valid day.

Both range endpoints are inclusive. `date` selects a single date and conflicts
with `datebefore`/`dateafter`; an inverted range is an error. ISO date options are
a native extension to the upstream strict CLI date grammar.

Entry dates use `metadata.upload_date`; if absent, a finite Unix `timestamp` is
converted in UTC. Unknown dates remain eligible, as with upstream date filters.
Use a metadata-presence condition if known dates are required. Invalid provided
dates are reported when date filtering is requested. Date filtering does not
silently convert malformed dates into missing values.

## Metadata filters

One `match_filter` expression supports `&` conjunction and scalar fields:

```text
duration>=60 & view_count<10K & !is_live
title^="Tutorial" & uploader!="Example"
id=12345 & ext=mp4
filesize<=?50MiB
age<=18 & rating>=4.5
```

Top-level `Media` fields include `id`, `title`, `url`, `source_url`, `ext`, `kind`,
and `mime_type`. Other simple field names address metadata keys. `age` falls back
to `age_limit`, and `rating` falls back to `average_rating`. These two aliases are
native conveniences. Format-dependent values are not invented from unselected
format alternatives.

- `field` tests presence; `!field` tests absence. Zero and empty strings are
  present. Booleans follow their truth value, so `!is_live` matches false or
  absent values.
- Comparisons support `=`, `<`, `<=`, `>`, and `>=`, plus negation such as `!=`
  or `!<`.
- Strings support contains `*=`, starts-with `^=`, and ends-with `$=`, also with
  negation. Quotes and ampersands can be escaped with a backslash. A quoted
  ampersand is also accepted without escaping as a native convenience.
- `?` after an operator includes unknown values, for example
  `view_count>?100`. Non-finite numbers are treated as unknown.
- Numeric values accept decimal sizes (`1K`/`1KB` = 1000), binary sizes
  (`1Ki`/`1KiB` = 1024), durations such as `2:30` or `1hour`, and decimal numbers.
  Units are case-sensitive, including upstream's legacy `1k`/`1kB` = 1024
  and `1kb`/`1Kb` = 1000 forms. The same rules apply through `Y` (yotta);
  malformed binary units such as `kiB` are rejected for numeric metadata.
  Explicit sizes round to the nearest whole byte, with ties to an even integer.
  Unsuffixed decimal comparisons retain their fractions; this intentionally
  differs from upstream match filters, which parse bare decimals as byte sizes
  and round them. For example, native `rating=4.5` compares against 4.5.
  Bare `m` is a size suffix, matching upstream; use `min` or `minutes` for time.
- Existing string values remain strings for comparisons, including numeric IDs.
  Lists and dictionaries cannot be compared as scalars.

Regex matching (`~=`), interactive prompts, Boolean OR within one expression,
repeated-filter OR policy, and break-on-rejection behavior are not implemented.
Unsupported expressions fail explicitly. No regex supplied by a user or remote
source is executed, and filters never evaluate Python code.

## Bounds and verification

Input is limited to 100,000 entries. Item expressions accept up to 8,192
characters, 1,024 segments, and absolute index/step values up to one trillion.
Large ranges are matched against existing positions rather than expanded into
large arrays. Match filters accept up to 8,192 characters and 128 conditions.

The tests cover source-index preservation, partial enumeration, positive and
negative slices, range steps, date boundaries, unknown metadata, escaping,
ordering, and input validation. Optional differential tests compare playlist,
date, and metadata-filter behavior with installed yt-dlp, including 600 seeded
playlist specifications. These comparisons verify the documented scope, not full
upstream collection or CLI parity.

Reference: [upstream selection options](https://github.com/yt-dlp/yt-dlp#usage-and-options).
