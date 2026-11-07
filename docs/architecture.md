# Architecture

The native implementation separates discovery, transfers, and orchestration. It imports no yt-dlp code during native operation. All native code uses the Python standard library; FFmpeg is an external process only when requested by the media protocol or conversion options.

| Module | Responsibility |
| --- | --- |
| `models` | Media-neutral records and actionable errors |
| `types` | MIME/extension category registry |
| `discovery` | Bounded HTTP probing, static page/feed/manifest extraction |
| `transfer` | Streaming HTTP transfers, validators, retries, hashing, atomic publication |
| `streams` | Restricted HTTP(S) FFmpeg input and audio/container conversion |
| `archive` | Transactional SQLite completion records with on-disk hash verification |
| `plugins` | Installed entry-point extractor dispatch |
| `engine` | Parallel item scheduling, archive and conversion composition |
| `cli` | Native options, explicit TOML config, JSON events and batch URLs |
| `compat` | Optional upstream passthrough; no native parity claim |

A `Media` record has `url`, `title`, `id`, `kind`, `ext`, `mime_type`, `source_url`, `headers`, and `metadata`. Native discovery returns a bounded list. Downloads return `DownloadResult(media, path, size, sha256, skipped)`. Feed/page traversal order is deterministic; concurrent completion order is not. The API returns successful results in input order.

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

Return an iterable of `omni_dlp.Media`. Use HTTP(S) media URLs and set `metadata={"protocol": "hls"}` or `{"protocol": "dash"}` for adaptive resources. Plugins are loaded in entry-point name order; the first supporting plugin handles a URL. Installed plugins are executable code, not sandboxed data. The transfer engine still applies its path and protocol restrictions.

## Data and failure handling

Native output names sanitize remote metadata. Relative template subdirectories stay within the selected output root. Per-destination locks serialize competing transfers. Native completion sidecars contain hashes rather than raw source URLs. JSON CLI output removes request headers, embedded URL credentials, query parameters, and fragments. A signed URL will therefore not round-trip through the inspection output; use original input URLs for actual downloads.

Cookies remain in the supplied cookie file and are loaded into a scoped cookie jar. Sensitive headers do not follow cross-origin redirects in the native HTTP client. FFmpeg has different redirect behavior, so native adaptive downloads reject credential headers and cookie files. The explicit compatibility command follows upstream behavior, including its logging/configuration behavior.

Completed files are published only after a successful transfer and integrity checks. Partials remain separate. Native archives record only completed downloads, and verify the recorded file before skipping work. A corrupt file produces an error unless replacement is explicitly requested. Errors do not count as successful archive entries.

## Migration strategy

The new core must grow through fixture-backed native implementations, with the inventory as a checklist. The compatibility bridge preserves access to upstream functionality while extractors are ported. Removing the bridge requires independent coverage of the tracked options and extractors, differential tests, live service checks, and cross-platform evidence. The initial implementation does not meet that release gate.
