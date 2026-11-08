# Native media sidecars

`omni_dlp.assets` plans and downloads subtitles, thumbnails, and descriptions
independently of video transfer. These use the native HTTP client's retries,
size limits, cancellation, scoped headers, cookies, output locks, checksums, and
completion receipts. No yt-dlp code is imported during native execution.

## Python interface

```python
from pathlib import Path
from omni_dlp.assets import AssetOptions, plan_assets, download_assets
from omni_dlp.transfer import TransferOptions

options = AssetOptions(
    write_subs=True,
    write_auto_subs=True,
    sub_langs="en.*,ja,-en-GB",
    sub_format="vtt/srt/best",
    write_thumbnail=True,
    write_description=True,
)
plans = plan_assets(media, Path("downloads/movie.mp4"), options)
results = download_assets(plans, TransferOptions(directory="downloads"))
```

`plan_assets` creates no files. Each `AssetPlan` contains its `media`, planned
`path`, optional `inline_text`, `kind`, and `language`. An optional `main_path`
records which main output must be protected. The engine can inspect every path
before starting any transfer. `download_assets` validates the entire plan list
before downloading, and returns ordinary `DownloadResult` records.

## Subtitle selection

`write_subs` enables manually supplied subtitles. `write_auto_subs` enables
`automatic_captions`. When both are enabled, manual entries take precedence for
the same language. Metadata supplies a mapping from language codes to ordered
track lists. Each selected track needs an HTTP(S) URL; its `ext` and optional
request `headers` are preserved.

Without `sub_langs`, exactly one language is selected: manual `en`, then the
first manual English variant, then any `en` or English variant, then the first
manual or available language. This matches the upstream default selection
strategy. It does not download every English variant automatically.

Language expressions are comma-separated and applied in order:

- `en.*,ja` uses case-insensitive full-match regular expressions.
- `all,-live_chat` selects all languages and excludes live chat.
- `glob:en-*` explicitly uses shell-style wildcard matching.
- `re:^en-[A-Z]+$` explicitly uses the supported regex syntax.
- An exclusion removes matches already selected. An expression containing only
  exclusions starts with an empty selection. Unmatched expressions add nothing.

The regex subset supports literals, escaped punctuation, `.`, character classes,
class ranges and negation, simple `*`, `+`, `?` quantifiers, and outer anchors.
Groups, alternation, counted repetition, lookarounds, escape classes, and
backreferences are rejected explicitly. The matcher uses bounded dynamic
programming rather than a backtracking regex engine. Pattern length, expression
count, input sizes, and matching work are limited. This is intentionally not yet
complete upstream regular-expression syntax parity.

Slash-separated format preferences select the last source-listed track matching
the first available preference. `best` means the last source-listed track. If
none of the preferences match, the last track is used and its metadata records
`format_fallback: true`, matching upstream's format fallback behavior. No subtitle
format conversion occurs. The native default is `vtt/srt/best`; upstream normally
uses `best`. Invalid URLs in a selected language's track list produce an error
instead of being silently discarded.

The behavior is covered by offline differential checks against upstream
[`process_subtitles`](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/YoutubeDL.py).

## Filenames and metadata

| Asset | Typical output beside `movie.mp4` |
| --- | --- |
| English WebVTT | `movie.en.vtt` |
| Selected thumbnail | `movie.jpg` |
| All thumbnails | `movie.low.jpg`, `movie.high.webp` |
| Description | `movie.description` |

If the main output is itself an image with the same filename, the single
thumbnail uses `movie.thumbnail.jpg`. Descriptions use a similar separate name
when the main file already ends in `.description`. Untrusted language/thumbnail
identifiers are sanitized with a deterministic digest suffix when necessary.
Conflicting names, including case-insensitive collisions and protected receipt
or partial-file paths, are rejected before transfers. Existing symlink outputs
and nested symlink directories are refused by the transfer engine.

The best thumbnail uses known image area, then dimensions, then source order.
`write_all_thumbnails` selects all distinct thumbnail URLs. Selected descriptions
are written as exact UTF-8 text, including valid empty descriptions. Missing
metadata yields no corresponding asset. Descriptions are capped at 8 MiB; subtitle
languages, tracks, and thumbnails have bounded metadata limits.

Parent credentials are not copied to foreign-origin assets. Explicit per-asset
headers apply to their target and undergo the HTTP client's redirect scoping.
Cookie files continue to use the domain/path-aware cookie jar. Completion receipts
store hashed resource identities, not raw signed URLs or request headers. Query
parameters remain part of the hashed asset identity because they can select
different resources. A refreshed signed URL therefore needs explicit overwrite
when it targets an existing filename; provider-specific stable asset identities
are not inferred by this generic module.

Descriptions are published atomically and their receipts bind the actual UTF-8
content digest. Repeated identical assets are checksum-verified before skipping.
Tampered assets or changed descriptions require explicit overwrite. Cancellation
before publication keeps the existing completed file and cleans the temporary
inline-text file. The main media file is never modified by the asset module.

## Current boundary

This implements sidecar downloading, not caption embedding, subtitle conversion,
automatic translation, image transcoding, or thumbnail embedding. `best` depends
on extractor-provided ordering; it is not a quality inference from subtitle
content. Network failures fail the asset operation rather than reporting partial
sidecar success as complete. Tests cover real local HTTP transfers, retries,
credentials, receipts, malformed metadata, path collisions, overwrite behavior,
and cancellation.
