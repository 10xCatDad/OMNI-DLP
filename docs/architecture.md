# Architecture

The native implementation separates discovery, selection, transfers, and
postprocessing. It imports no yt-dlp code during native operation. Native Python
modules use the standard library. FFmpeg handles supported adaptive transfers,
merging, conversion, and embedding; ffprobe verifies embedded stream and metadata
preservation. Arbitrary downloaded JavaScript is not executed.

| Module | Responsibility |
| --- | --- |
| `models` | Media-neutral records, selectable formats and actionable errors |
| `extractors` | Independent site parsers and a bounded, credential-scoped HTTP client |
| `manifests` | Native HLS variant/audio-group parsing and supported AES-128 descriptors |
| `selection` | Bounded format expression/filter/sort implementation |
| `collection_selection` | Source indices, bounded ranges/slices, ordering, dates, scalar metadata filters and incomplete-enumeration checks |
| `merge` | Verified component downloads and local FFmpeg stream assembly |
| `assets` | Pure sidecar planning, bounded subtitle language matching, thumbnails, descriptions and native transfer |
| `metadata` | Retained embedding copies, validated tags/chapters/subtitles and ffprobe verification |
| `infojson` | Bounded metadata-sidecar publication, logical ownership receipts and verified updates |
| `types` | MIME/extension category registry |
| `discovery` | Bounded HTTP probing, static page/feed/manifest extraction |
| `transfer` | Streaming HTTP transfers, validators, retries, hashing, atomic publication |
| `streams` | Bounded manifest/key preflight, HTTP(S) FFmpeg adaptive input including ordinary AES-128 HLS, and audio/container conversion |
| `archive` | Transactional SQLite completion records with on-disk hash verification |
| `plugins` | Installed entry-point extractor dispatch |
| `engine` | Per-source collection selection, output collision checks, parallel scheduling, archives, sidecars and postprocessing composition |
| `cli` | Native options, explicit TOML config, JSON events and batch URLs |
| `compat` | Optional upstream passthrough; no native parity claim |
| `youtube_challenges` | Experimental bounded signature compiler; separately tested and not yet connected to extraction |

A `Media` record has `url`, `title`, `id`, `kind`, `ext`, `mime_type`, `source_url`,
`headers`, and `metadata`. It also carries `formats` and `requested_formats`,
lists of typed `Format` records. Native discovery returns a bounded list.
`Downloader.inspect()` applies requested collection selection per input source
before expanding HLS manifests. Original indices and reported counts survive
filtering; unknown totals are not replaced with the length of a loaded prefix.
Duplicate source positions currently raise an error, including multiple assets
that an extractor associates with one collection entry.

`Downloader.prepare()` selects representations; `fetch()` also prepares records
automatically. Logical extractor/resource identities distinguish formats while
surviving signed-URL refreshes. Archives also distinguish effective adaptive/merge
containers. Downloads return `DownloadResult(media, path, size, sha256, skipped,
attachments)`, with sidecar results in `attachments`. Feed/page traversal order
is deterministic; concurrent completion order is not. The API returns successful
results in input order.

Asset planning computes destinations before network transfer so the engine can
reject collisions among main media, retained conversions, sidecars and embedded
copies. Completed or verified archived media can acquire newly requested
sidecars; embedding then creates a separate verified output. `fetch_assets()`
implements sidecar-only downloading without requiring a playable main format.
Supported metadata-only extractors mark placeholder records with
`formats_required=True`, so unavailable playback never falls back to downloading
the webpage as media.

## Plugin contract

Declare an entry point in an installed package:

```toml
[project.entry-points."omni_dlp.extractors"]
example = "example_plugin:extractor"
```

The loaded object must implement:

```python
def supports(url: str) -> bool: ...
def discover(url: str, *, headers, cookies, timeout, media_types, max_items): ...
```

Return an iterable of `omni_dlp.Media`. Use HTTP(S) media URLs and set
`metadata={"protocol": "hls"}` or `{"protocol": "dash"}` for adaptive resources.
Provide original `playlist_index` and trustworthy `playlist_count` when known;
mark incomplete enumeration with `collection_complete=False`. When collection
selection is active, the engine requests unfiltered discovery and applies
media-kind filtering after assigning source positions. Built-in extractors
still receive requested media kinds to expose their explicit artwork/caption
modes and supply collection positions themselves. Plugins are loaded in
entry-point name order; the first supporting plugin handles a URL. Installed
plugins are executable code, not sandboxed data. The transfer engine still
applies its path and protocol restrictions.

## Data and failure handling

Native output names sanitize remote metadata. Relative template subdirectories stay within the selected output root. Per-destination locks serialize competing transfers. Native completion sidecars contain hashes rather than raw source URLs. JSON CLI output removes request headers, embedded URL credentials, query parameters, and fragments. A signed URL will therefore not round-trip through the inspection output; use original input URLs for actual downloads.

Cookies remain in the supplied cookie file and are loaded into a scoped cookie jar. Sensitive headers do not follow cross-origin redirects in the native HTTP client. FFmpeg has different redirect behavior, so native adaptive downloads reject credential headers and cookie files. The explicit compatibility command follows upstream behavior, including its logging/configuration behavior.

Supported HLS encryption is ordinary identity AES-128 with HTTP(S) key delivery,
not a DRM license workflow. The initial manifest/key inspection is bounded;
FFmpeg refetches manifests and may reload live playlists, so this is not an
immutable validated snapshot. Completion receipts do not store key bytes or
raw key URLs. See [HLS boundaries](hls.md).

Completed files are published only after a successful transfer and integrity checks. Partials remain separate. Native archives record only completed downloads, and verify the recorded file before skipping work. A corrupt file produces an error unless replacement is explicitly requested. Errors do not count as successful archive entries.

Info JSON has a separate content/identity receipt. The CLI redacts its record
before publication; the generic writer does not independently scrub secrets.
Only verified sidecars belonging to the same logical media can refresh without
explicit overwrite. Metadata and receipt publication are individually atomic,
not one transaction; a crash between them can require explicit recovery. See
[info JSON ownership and filesystem limits](info-json.md).

## Migration strategy

The new core must grow through fixture-backed native implementations, with the inventory as a checklist. The compatibility bridge preserves access to upstream functionality while extractors are ported. Removing the bridge requires independent coverage of the tracked options and extractors, differential tests, live service checks, and cross-platform evidence. The initial implementation does not meet that release gate.
