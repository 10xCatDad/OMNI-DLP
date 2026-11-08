# Capability and parity ledger

This is an early implementation of a replacement, not a completed replacement.
The pinned upstream declaration inventories are exhaustive within their stated
scope. Native behavior below is independently implemented; `compat` runs an
optional upstream installation. Similar flag names do not imply equal semantics.

| Capability | Native status | Evidence / boundary |
| --- | --- | --- |
| Any HTTP(S) file bytes | Implemented | No codec requirement; `--direct` bypasses discovery for raw JSON/XML and other files |
| Media categories | Implemented | Image, audio, video, document, archive, font, model, subtitle, other |
| Static galleries and mixed pages | Implemented | HTML media/source/picture/srcset/lazy attributes, metadata and JSON-LD |
| RSS/Atom podcasts and JSON Feed | Implemented | Enclosures/attachments, relative links, deduplication |
| JSON media lists | Implemented | Explicit media-array manifest; no arbitrary filesystem access |
| HTTP discovery | Implemented | HEAD/probe, MIME and extension, bounded document reads |
| Resumable direct transfers | Implemented | Source/partial digests, ETag or modification date, checked Content-Range |
| Retries, speed, size | Implemented for direct HTTP | Per-item limits; bounded retries and Retry-After |
| Disk-space preflight | Implemented for known-size HTTP | Free-space estimate is advisory; concurrent changes can still cause ENOSPC |
| Cancellation | Implemented | Ctrl-C stops workers and FFmpeg; stalled HTTP I/O remains bounded by socket timeout |
| Concurrency | Implemented | Item-level worker pool and destination locks; no fragment concurrency |
| Output filenames | Implemented subset | title, id, ext, kind, playlist_index; confined relative paths |
| Integrity and archives | Implemented | SHA-256 completion sidecars and SQLite archive with file verification |
| Headers and cookies | Implemented for direct HTTP | Netscape file cookies; sensitive headers scoped by origin |
| Native HLS/DASH | Implemented subset via FFmpeg | Clear HLS/DASH and ordinary identity AES-128 HLS with HTTP(S) keys; native HLS variants/audio groups; [protocol and protection limits](hls.md) |
| Adaptive recovery/options | Limited | Restart on interruption; no fragment resume, native retry policy, rate limit, credential headers or cookie files |
| Adaptive completeness | Conservative | Missing segments and other FFmpeg diagnostics fail the job; only one exact HLS MIME diagnostic is permitted on successful exit |
| Audio conversion | Implemented via FFmpeg | MP3, M4A, M4B, AAC, FLAC, Opus, Ogg, WAV, AIFF; codec availability depends on FFmpeg build |
| Container remux | Implemented via FFmpeg | CLI MP4, MKV, WebM, MOV; source preserved; container must support codecs |
| JSON inspection/metadata | Implemented | Credentials/queries redacted; not a lossless extraction manifest |
| Verified info JSON sidecars | Implemented | Content/identity receipts, bounded reads, locks and atomic file publication; edited or unrelated files require explicit replacement; [ownership and crash boundaries](info-json.md) |
| Metadata and sidecars without main media | Implemented subset | `--skip-download`; supported extractors retain available public metadata/assets even when playback is unavailable; ordinary media transfer still fails without usable formats |
| Structured progress | Partial | Flushed completion/error events; byte progress, totals, and phase events pending |
| Batch files and explicit TOML | Implemented | Native schema; no upstream config interoperability |
| Extractor plugins | Implemented new API | Installed Python entry points; not yt-dlp plugin-compatible |
| Native site-specific extractors | Partial | Seven families, nine upstream handlers partially covered; see [site ledger](sites/README.md) for per-service evidence and gaps |
| Upstream CLI and site execution | Delegated | `omni-dlp compat ...`; all arguments passed to separately installed yt-dlp |
| Upstream Python API | Delegated | Explicit adapter, not an API-compatible native replacement |
| Format selectors/sorting | Substantial native subset | IDs, aliases, filters, groups, fallbacks, merges and sorting; differential tests and [remaining grammar gaps](format-selection.md) |
| Collection selection and metadata filters | Substantial native subset | Inclusive ranges/slices, negative indices with known totals, ordering, dates and scalar filters; incomplete discovery remains explicit; [syntax and limits](collection-selection.md) |
| Site authentication/browser cookies/impersonation | Not ported | Use upstream features where available through compatibility command |
| Captions, chapters and thumbnails as site metadata | Partial | Coverage depends on each extractor; universal discovery and HLS subtitle-rendition extraction remain incomplete |
| Subtitle, thumbnail and description sidecars | Implemented subset | Native HTTP transfer, automatic/manual caption selection, bounded language patterns, subtitle-format fallback, thumbnail selection and UTF-8 descriptions; [asset rules](assets.md) |
| Metadata, chapter and subtitle embedding | Implemented subset via FFmpeg/ffprobe | Retained `.embedded` copy, allowlisted tags, validated chapter intervals, MP4/MKV text subtitles and stream verification; [container matrix](metadata.md) |
| Subtitle conversion and thumbnail embedding | Not ported generally | Text subtitle conversion occurs only when embedding into supported MP4-family containers; no standalone conversion, translation or cover-art embedding |
| Comments | Not ported | Compatibility command |
| Selected audio/video/subtitle merging | Implemented via FFmpeg | Native component downloads, content-bound receipts, cancellation, explicit container and retained inputs |
| SponsorBlock and chapter editing | Not ported | Compatibility command |
| RTMP, F4M, ISM, websocket/live-chat/MHTML | Not ported | Compatibility command where supported upstream |
| General JavaScript/browser execution | Not implemented | Static parsing only; an experimental bounded YouTube signature compiler is tested separately and is not yet connected to extraction |
| Native self-update/standalone executables | Not implemented | Install package from source or wheel |
| DRM/protected content access | Not promised | Access and decryption cannot be guaranteed by a downloader |
| Every live site / every media codec | Not verified | Registry entries and opaque downloads do not establish live extraction or conversion coverage |

## Additions linked to upstream requests

AIFF and M4B output have actual codec/container tests. These address the basic
format choices requested in [#15295](https://github.com/yt-dlp/yt-dlp/issues/15295)
and [#11761](https://github.com/yt-dlp/yt-dlp/issues/11761), not every request about
cover art, audiobook metadata, or chapter preservation. Uniform image transfer
retry/integrity behavior supports the design concern in
[#15242](https://github.com/yt-dlp/yt-dlp/issues/15242). Known-size disk preflight
addresses a portion of [#15714](https://github.com/yt-dlp/yt-dlp/issues/15714).
Flushed item events address part of
[#13649](https://github.com/yt-dlp/yt-dlp/issues/13649); they are not yet a complete
streaming progress API.

## Release gates remaining

1. Native extractor ports with per-site fixtures, authenticated tests where
   available, and an explicit supported URL matrix.
2. Complete format selection/sort grammar, collection traversal/filter parity,
   metadata fields and differential coverage. Collection selection currently
   rejects multiple records sharing one source index, including some mixed
   artwork/audio results and animated-gallery variants.
3. Broader subtitle conversion and cover-art processing, multistream policies,
   protocols, authentication, and plugin migration.
4. Broad platform and live-service validation, crash recovery and large-file
   testing, performance measurements, standalone packaging, and update policy.
5. Complete the per-option and per-extractor migration ledger before claiming
   independent parity or removing the compatibility dependency.

Nine upstream handler interfaces have partial independent implementations across
seven native extractor families. None is claimed to have complete upstream parity.
The remaining site library is unported. The compatibility bridge does not increase
independent coverage. [Machine-readable migration ledger](native-coverage.json).
