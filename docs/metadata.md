# Native metadata and subtitle embedding

`--embed-metadata`, `--embed-chapters`, and `--embed-subs` are independent native
operations. They use installed FFmpeg and ffprobe, without calling yt-dlp.

```sh
omni-dlp --embed-metadata --embed-chapters 'https://example.org/watch/123'
omni-dlp --write-subs --sub-langs en,es --embed-subs 'https://example.org/watch/123'
```

The native extractor must supply the requested metadata or subtitle URLs.
Embedding does not discover missing captions or synthesize chapters. Subtitle
files are downloaded through the normal asset pipeline before embedding.

A completed source such as `movie.mkv` remains intact. The operation writes
`movie.embedded.mkv` beside it. Names near filesystem limits are shortened while
retaining the `.embedded` suffix. If no requested metadata, chapters, or selected
subtitle files are available, it returns the original result without creating an
empty embedding artifact. Existing output is skipped only when its receipt and
content hash match. Replacing changed embedded output requires an explicit
`--force-overwrites`.

## Containers and fields

| Container | Tags | New chapters | New subtitles |
| --- | --- | --- | --- |
| MP4, M4V, MOV | Supported | Supported | Converted to `mov_text` |
| M4A, M4B | Supported | Supported | Explicitly unsupported |
| MKV, MKA | Supported | Supported | SRT, WebVTT, ASS/SSA copied as text subtitle streams |
| MP3 | Supported | Supported through ID3 chapters | Explicitly unsupported |
| FLAC, Ogg, Opus | Supported | Explicitly unsupported in the native operation | Explicitly unsupported |
| WAV, AIFF/AIF | Supported | Explicitly unsupported | Explicitly unsupported |

Other containers fail with an actionable remux instruction. Unsupported codecs
inside an otherwise supported container also fail; media is not silently dropped
to make a mux succeed. MP4 timed-text conversion can reduce styling from rich ASS
subtitles. MKV preserves the original supported text subtitle codec.

The new descriptive fields are title, artist, album, description, date, and
uploader. Artist falls back to uploader when absent. Date can come from `date`,
`upload_date`, or `release_date`. Vorbis-comment containers and WAV store
description as `comment`; an imported description therefore replaces the existing
comment. WAV lacks a standard uploader tag. That omission is recorded in
`result.media.metadata["embedding"]["omitted_tags"]`; uploader can still supply
the artist fallback. AIFF writes ID3v2 metadata. Other original metadata is
preserved unless a supported new field replaces it.

Only these allowlisted fields are imported from extraction metadata. Headers,
cookies, request URLs, and arbitrary metadata dictionaries are not converted to
tags. URL-bearing values and recognizable credential assignments in newly
imported text are redacted. This is not a generic secret scanner: arbitrary secret
strings cannot be distinguished from ordinary prose. Existing tags already
inside the source file remain present unless replaced; embedding does not erase
preexisting metadata.

## Chapters and subtitles

Chapter entries use `start_time`, `end_time`, and `title`. Times are seconds and
are encoded with millisecond precision. Starts must increase; intervals must
have positive duration and cannot overlap. An omitted end is inferred from the
next chapter start or the declared media duration. A final chapter without either
an end or duration is rejected. Nonfinite, negative, boolean, out-of-range, and
submillisecond zero-length intervals are rejected before starting FFmpeg.

Valid new chapters replace the source chapter list. When no new chapters are
requested or available, existing chapters are preserved. Source timestamps are
retained to avoid moving chapter boundaries by codec delay during a tag update.

Existing streams, including subtitles and attachments, are mapped explicitly and
copied. Newly requested subtitles are appended. Each new subtitle gets its own
codec and language setting, so adding an MP4 subtitle does not convert all
existing subtitle tracks. Two-letter language codes are normalized to ISO 639-2/T
identifiers; region variants such as `en-US` use the base language `eng`. Missing
or unknown two-letter codes use `und`. Valid three-letter codes pass through.

FFprobe verifies stream preservation, subtitle counts and languages, requested
tags, and chapter timing/title metadata before the temporary output is published.
Source files, subtitle files, and the destination remain locked during the
operation. Cancellation stops the subprocess and removes temporary output. An
optional output-size limit also prevents publication of an oversized result.

## Python API

```python
from omni_dlp.metadata import embed_metadata, plan_output

# downloaded is a DownloadResult; captions contains downloaded subtitle results.
embedded = embed_metadata(
    downloaded,
    subtitles=captions,
    embed_tags=True,
    embed_chapters=True,
    embed_subtitles=True,
    overwrite=False,
)
print(plan_output(downloaded.path), embedded.sha256)
```

The completion receipt hashes source bytes, subtitle bytes, normalized metadata,
languages, container, and enabled operations. Raw metadata values and source URLs
are not stored in the receipt. Modified source data or embedding settings cannot
reuse a stale output silently. API attachment records remain attached to the
returned result.

## Evidence and references

Offline tests generate actual audio/video fixtures and inspect the embedded
outputs. They cover common audio containers, chapter and tag escaping, trailing
backslashes, chapter-injection attempts, subtitle language/codec handling,
existing-stream preservation, credential redaction, receipt invalidation,
unsupported formats, local-playlist rejection, locking, size limits, and real
FFmpeg cancellation. These checks do not certify every FFmpeg release, codec,
player's display behavior, or website's metadata coverage.

Format behavior follows the [FFmpeg metadata format documentation](https://ffmpeg.org/ffmpeg-formats.html#Metadata-1)
and [stream mapping and metadata options](https://ffmpeg.org/ffmpeg.html#Advanced-options).
Language identifiers follow the [ISO 639-2 code list](https://www.loc.gov/standards/iso639-2/php/code_list.php).
