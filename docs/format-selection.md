# Native format selection

Native extractors can describe alternative representations using `Media.formats`.
Each `Format` records its URL, ID, extension, codecs, protocol, dimensions,
framerate, bitrate, size, language, request headers, and optional metadata.
Selection operates on these records without importing or invoking yt-dlp.

The native selector implements a substantial subset of the upstream format
language. It is not a claim of complete option or sorting parity. The independent
parser is tested against the installed optional upstream package with fixed and
seeded format fixtures. Tests do not establish that every live format is usable.

## Expressions

| Expression | Result |
| --- | --- |
| `best` / `b` | Best combined video/audio representation |
| `worst` / `w` | Worst combined video/audio representation |
| `bestvideo` / `bv` | Best video-only representation |
| `bestaudio` / `ba` | Best audio-only representation |
| `worstvideo` / `wv`, `worstaudio` / `wa` | Corresponding worst representation |
| `bv*`, `ba*` | Best representation containing that stream, possibly containing both |
| `best*`, `worst*` | Best/worst representation containing either stream |
| `bv.2`, `ba.2` | Second best matching representation; positive indices are supported |
| `22`, `audio-main` | Exact extractor-provided format ID |
| `mp4`, `m4a`, `webm` | Best matching extension, using the upstream combined/separate-stream rules |
| `bv+ba/best` | Merge the best separate video and audio, or fall back to a combined representation |
| `bv,ba` | Produce two separate outputs |
| `(bv,ba)/best` | Parenthesized selectors with explicit precedence |
| `all` | Separate outputs for all formats, ordered best to worst |

Operators bind in this order: parentheses, merge `+`, fallback `/`, separate
outputs `,`. Each fallback succeeds if it yields any outputs. A group filter
restricts its input candidates before evaluating the group. For example,
`(bv+ba)[height<=720]` excludes audio formats with unknown height, so the merge
may be empty; `(bv+ba)[height<=?720]` retains those unknown-height candidates.

Only explicit `vcodec="none"` or `acodec="none"` marks a stream absent. Empty
codecs are unknown, following upstream selection semantics. `best` and `worst`
fall back to the sole media type when the entire collection is audio-only or
video-only. Native selection also extends those selectors to entirely non-AV
collections such as image variants. Image, document, archive, font, model and
subtitle extensions in the media registry are selectable directly.

A merge keeps at most one video-bearing and one audio-bearing representation,
in expression order. A later representation with a stream already present is
dropped as a whole, matching the default upstream single-stream behavior.
`best+ba` therefore retains the combined format alone. Multistream policies and
`mergeall` are not implemented; `mergeall` raises an explicit error.

When both formats identify an HLS master through `metadata.manifest_url`,
`metadata.audio_group` constrains selection to compatible rendition groups from
that master. The same group labels on unrelated masters do not constrain each
other.

## Filters

Filters chain with logical AND:

```text
bv[height<=720][fps<=30]+ba
best[filesize<50M]/worst
bv[format_id^=video-][ext!=webm]
ba[language^="en"][format_note!*="description"]
```

Numeric comparisons are `=`, `!=`, `<`, `<=`, `>`, and `>=`. Filter size suffixes
are case-sensitive: `1K`/`1KB` = 1000 and `1M`/`1MB` = 1000000;
`1Ki`/`1KiB` = 1024 and `1Mi`/`1MiB` = 1048576. Upstream's legacy lowercase
forms are preserved: `1k`/`1kB` = 1024, but `1kb`/`1Kb` = 1000. The same rules
apply through `Y` (yotta). A standalone `B`/`b` byte suffix is a native extension.
Sizes round to the nearest whole byte, including ties to an even integer;
unsuffixed numbers retain decimals. Malformed unit case such as `kiB` raises an
error. Numeric fields include dimensions,
framerate, bitrate, filesize/approximate filesize, duration, sample rate, audio
channels, quality, and source/language preferences. Unknown or non-finite values
fail numeric filters unless `?` follows the operator, such as `[height<=?720]`.
Zero is a known value, not an unknown value.

String comparisons support equality, inequality, starts-with `^=`, ends-with
`$=`, and contains `*=`. The last three also support negation: `!^=`, `!$=`, and
`!*=`. String fields include ID, extension, protocol, codecs, language, format
note, container, resolution, and dynamic range. Quote values containing spaces
or expression punctuation. Regex matching and arbitrary custom filter fields
are not supported and raise errors.

## Sorting

The native `sort_formats(formats, sort=None)` function returns a new list from
worst to best. Selection uses that ranking; it does not reorder the caller's
list in place.

The comma-separated sort fields include `height`, `width`, `res`, `fps`, `tbr`,
`abr`, `vbr`, `br`, `filesize`, `fs_approx`, `size`, `asr`, `channels`, `vcodec`,
`acodec`, `codec`, `proto`, `vext`, `aext`, `ext`, `hdr`, `quality`, `source`,
`lang`, `hasvid`, `hasaud`, `ie_pref`, and `id`. Metadata preferences use numeric
extractor values; `lang` is not alphabetical language-name sorting.

- `height,fps` prefers greater height and then greater framerate.
- `+height` prefers smaller known heights.
- `height:720` prefers the greatest height at or below 720, otherwise the
  smallest height above 720.
- `+height:720` reverses that threshold preference.
- `filesize~1G` prefers the closest known size to 1 GiB.
- `vcodec:avc` prefers the codec rank at or below H.264; codec-family aliases
  such as `avc1`, `hvc1`, and `av01` are recognized.
- `codec:avc:mp4a` supplies separate video/audio preferences.

Size-sort targets use upstream's separate binary convention for bare suffixes:
`filesize:1K` and `filesize:1k` both target 1024 bytes. As a native extension,
explicit byte suffixes also work in sort targets and follow filter rules:
`filesize:1KB` targets 1000 bytes and `filesize:1KiB` targets 1024 bytes.

Unknown values rank below known values even for ascending sorts. `hasvid` and
`ie_pref` retain highest priority. Remaining default fields break ties after
user-provided fields. Default dynamic-range ranking prefers HDR12 or below over
Dolby Vision. Exact nearest-value ties prefer the lower value, or the higher
value with a `+` sort. `res` uses the smaller known dimension for portrait media.

`--format-sort-force`, extractor-specific sort overrides, free-format preference,
network availability checks, and every upstream historical alias are not yet
implemented. The native sort is tested against selected upstream behaviors,
not declared bit-for-bit equivalent for every metadata combination.

## Python API and bounds

```python
from omni_dlp.selection import select_formats, sort_formats

outputs = select_formats(media.formats, "bv[height<=1080]+ba/b", "fps")
for components in outputs:
    # One output; multiple components require the merger.
    print([fmt.format_id for fmt in components])
```

A valid expression with no candidates returns `[]`. Malformed or unsupported
syntax raises `OmniError`; it does not silently select an unrelated default.
Parsing never executes code. Expressions are limited to 8,192 characters,
48 nesting levels, 512 selector nodes, 64 explicit sort fields, 10,000 input
formats, and 4,096 output groups to bound combinatorial expansion.

Reference: [upstream format selection and sorting documentation](https://github.com/yt-dlp/yt-dlp#format-selection).
