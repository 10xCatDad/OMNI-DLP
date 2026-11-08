# Round 3: native collections, assets, metadata, and additional sites

Release: **0.3.0a1**. This closes the current implementation round, not the full
yt-dlp rewrite. The native downloader remains an early alpha with partial site
coverage. The explicit `compat` command is separate upstream execution and never
counts as independently rewritten functionality.

## Completed in this round

| Area | Implemented behavior | Details |
| --- | --- | --- |
| Collections | Source-position ranges and slices, reverse/seeded random order, date ranges, bounded scalar metadata filters | [Selection](collection-selection.md) |
| Media sidecars | Manual/automatic captions, language and format selection, thumbnails, descriptions, asset listing, and sidecar-only downloads | [Assets](assets.md) |
| Metadata | Tags, validated chapters, selected subtitle embedding, retained source files and verified derived outputs | [Embedding](metadata.md) |
| Info JSON | Atomic, locked updates; ownership and byte verification; user-edited or unrelated files preserved by default | [Info JSON](info-json.md) |
| HLS | Ordinary AES-128 identity-key streams, explicit/implicit IVs and rotated keys; exact benign MIME-warning handling | [HLS](hls.md) |
| Reddit | Public post/gallery parsers, original images and GIF/MP4 assets, hosted HLS/DASH and fallback video, bounded crossposts | [Reddit](sites/reddit.md) |
| SoundCloud | Public track and bounded set extraction, progressive/HLS audio, artwork and metadata | [SoundCloud](sites/soundcloud.md) |
| Metadata-only extraction | Public YouTube, Vimeo, Bandcamp, Reddit, and SoundCloud metadata/assets can remain usable when supported main formats are absent | [Site coverage](sites/README.md) |
| Reliability | Actual archived paths participate in collection-wide collision checks; missing sidecars can be recovered beside older outputs; metadata-only placeholders cannot download page HTML | [Validation](validation.md) |
| Format selection | Correct filter-size unit dialect and byte-rounding boundaries, separate from sort-size semantics | [Formats](format-selection.md) |

The native Python API exposes collection and asset options through `Options`.
Main `DownloadResult` records include an `attachments` list. Archive hits verify
the main file and still perform requested asset/embedding work. Auxiliary failure
does not become a successful complete-download event; completed source bytes
remain available for retry. All planned files, receipts, locks, metadata sidecars,
archive storage, and the configured manifest are checked for overlap.

## Research completed but not enabled for downloads

An experimental static YouTube signature compiler derives transformations from
captured public player text without evaluating downloaded JavaScript. Its corpus
script verifies three pinned public vectors from two player assets. This is
useful research evidence, not production signature support.

Independent review found that recognizing a transform does not prove its role
in the player, and indirect JavaScript mutations can invalidate static reasoning.
The compiler therefore remains disconnected from native extraction. Linking the
actual signature call path and proving the accepted semantic subset are required
before integration. See [YouTube challenge research](research/YOUTUBE-CHALLENGES.md)
and [YouTube coverage](sites/youtube.md).

## What remains incomplete

The pinned [upstream inventory](research/README.md) remains the migration baseline:
323 option definitions, 394 option aliases, 1,754 listed extractor handlers,
22 downloader implementations, and 29 postprocessors. The issue snapshot covers
2,074 open issues after excluding pull requests. These counts describe the
recorded upstream snapshot; they are neither current live-site guarantees nor
implemented-native counts.

- Most upstream extractors and site-specific authentication, search, account
  libraries, comments, and pagination workflows have not been rewritten.
- Native YouTube still lacks encrypted signature solving, `n` challenge solving,
  PO-token generation, SABR, and full collection handling. Live playback remains
  unverified. Bandcamp live playback remains unverified; Reddit post JSON was
  blocked in the recorded environment even though hosted video downloading worked.
- Subtitle conversion/translation, thumbnail embedding, chapter splitting,
  SponsorBlock, arbitrary postprocessor chains, browser-cookie import, proxies,
  impersonation, and the full upstream configuration/template/option surface
  remain pending. Capability-specific boundaries are in [capabilities](capabilities.md).
- Native segmented downloading with fragment retry/resume and isolation of all
  manifest/segment/key requests is pending. HLS/DASH currently use FFmpeg. DRM,
  SAMPLE-AES, and authenticated adaptive streams are unsupported.
- Collection filter grammar is a documented subset. Multiple asset records
  sharing one source index can be rejected as ambiguous; source-level grouping
  needs a designed interface. Collection completeness does not certify every
  file transfer or a stable snapshot of a changing site.
- Generic assets retain full query strings in hashed resource identities. Signed
  subtitle/thumbnail URL renewal may require explicit overwrite. Provider-supplied
  stable track identities are a future improvement; stripping arbitrary query
  strings would risk reusing the wrong asset.
- Image format conversion, decoded-content or magic-byte validation, recursive
  crawling, multi-hour live-stream qualification, and large-scale service testing
  remain pending. Arbitrary HTTP files can be downloaded as bytes today, which
  is separate from understanding every media format.

## Compatibility and upgrade notes

This release adds fields to native JSON output and remains alpha. Info JSON
written by older versions has no ownership receipt: replacing it now requires
`--force-overwrites`. Stable identities for native image/document records without
format alternatives are now propagated consistently; older receipts keyed only
by URL may also need a new destination or explicit overwrite on the first run.
Source and derived media files are retained by design, so embedding and conversion
consume additional disk space.

Existing tests, new local HTTP/FFmpeg integration tests, exact corpus results,
platform CI, and distribution checks are recorded in [validation](validation.md).
Live-site checks are recorded separately in their site documents. Neither the
test count nor this release establishes full yt-dlp parity.

## Next implementation priorities

1. Resolve signature-role association and the experimental compiler's semantic
   boundaries, then validate a real native YouTube download before claiming support.
2. Implement a native adaptive-transfer layer with request isolation, fragment
   integrity, retries/resume, and properly scoped authentication.
3. Add source-level grouping for multiple assets per collection entry and explicit
   stable identities for provider captions/artwork.
4. Expand extractors and postprocessors according to the pinned feature/issue
   inventory, recording fixture coverage and live evidence separately.
