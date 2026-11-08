# OMNI-DLP

Download images, audio, video, documents, archives, fonts, models, and other HTTP files through one media-neutral interface.

**Status: early alpha, incomplete rewrite.** The native core is new Python code with no runtime dependencies. It includes partial native implementations for YouTube, Vimeo, Bandcamp, Internet Archive, Wikimedia, Reddit, and SoundCloud. Native operations include format and collection selection, multistream merging, media sidecars, and metadata embedding. It does **not** yet replace yt-dlp's full site library or option surface. An explicit optional `compat` command runs yt-dlp unchanged while native coverage grows. Delegation is not native feature parity.

See the [0.3 round report](docs/round-3.md) for completed work, remaining gaps, and the next implementation priorities.

## Install

Python 3.11 or newer:

```sh
python -m pip install .
omni-dlp --help
```

For existing yt-dlp sites and options:

```sh
python -m pip install '.[compat]'
omni-dlp compat --help
omni-dlp compat -f 'bv*+ba/b' --write-subs 'https://example.org/watch/123'
```

FFmpeg must be on `PATH` for HLS/DASH, audio conversion, and container remuxing. Metadata embedding also requires ffprobe. Install these with your operating system's package manager. Direct file downloads and page discovery do not require them.

## Native usage

```sh
# Direct media or a static page containing mixed media
omni-dlp 'https://example.org/gallery/' -P downloads --jobs 4

# Only images, including picture/srcset and common lazy-loading attributes
omni-dlp --media-type image 'https://example.org/gallery/'

# Download a JSON/XML file as raw bytes instead of parsing it
omni-dlp --direct 'https://example.org/data.json'

# Inspect without downloading; metadata URLs are credential-redacted
omni-dlp inspect 'https://example.org/podcast.xml'

# Resume downloads, track verified completion, write a JSONL manifest
omni-dlp -a urls.txt --download-archive archive.sqlite --manifest downloads.jsonl

# Convert an audio file, preserving the original
omni-dlp 'https://example.org/song.flac' -x --audio-format aiff

# Finite HLS, including ordinary AES-128, or clear DASH
omni-dlp 'https://example.org/stream.m3u8'

# Select source positions and filter by available upload dates
omni-dlp --playlist-items '1:10:2' --dateafter 20250101 'https://example.org/podcast.xml'

# Download captions, artwork, and descriptions without the main media
omni-dlp --skip-download --write-subs --sub-langs 'en.*,ja' \
  --write-thumbnail --write-description --write-info-json 'https://example.org/watch/123'

# Retain the source and write a separate copy with tags, chapters, and subtitles
omni-dlp --embed-metadata --embed-chapters --embed-subs 'https://example.org/watch/123'
```

These are example addresses. `--dump-json` and `--simulate` also perform discovery without downloading. `--progress-json` writes flushed item-completion and error events to stderr. This is not byte-level progress reporting. `--ignore-errors` continues other items but returns a nonzero exit status if anything fails.

Native discovery handles direct HTTP(S) resources, static HTML media and metadata, RSS/Atom enclosures, JSON Feed attachments, and JSON media manifests. Generic discovery does not execute JavaScript, solve authentication challenges, or recursively crawl websites. Built-in extractors can enumerate their documented collections within explicit bounds. Images are full downloadable assets rather than merely video thumbnails. Known file types get useful categories; arbitrary HTTP files remain downloadable as `other`. Server MIME metadata and URL extensions drive classification; content decoding and magic-byte validation are not implemented.

The native transfer engine streams to temporary files, validates range responses and resource validators, checks truncation, calculates SHA-256, and publishes completed files atomically. It supports bounded retries, per-transfer speed and size limits, headers, Netscape cookies, and safe output templates. Resume requires a matching partial record and server support; unsafe appends are rejected. Ctrl-C cancels queued jobs, interrupts rate/retry waits, and stops FFmpeg. An active stalled HTTP request can take up to the socket timeout to stop. A completed file is skipped only when its source identity and digest match. If an existing file fails verification, choose a new path or explicitly use `--force-overwrites`.

Output templates support the native record fields, for example `%(kind)s/%(title)s [%(id)s].%(ext)s`. See [capabilities](docs/capabilities.md) for precise boundaries, adaptive-stream restrictions, and migration milestones.

## Native sites and formats

```sh
omni-dlp --list-extractors
omni-dlp -F 'https://archive.org/details/testmp3testfile'
omni-dlp -f 'bestaudio[ext=mp3]/bestaudio' 'https://archive.org/details/testmp3testfile'
omni-dlp -f original 'https://commons.wikimedia.org/wiki/File:Example.webm'
```

`-f` supports format IDs, filters, `+` merging, `/` fallbacks, and comma-separated
outputs. `-S` orders formats; `--merge-output-format` selects the merged container.
Audio/video components are retained and verified before local FFmpeg merging.
Use `%(format_id)s` in custom filenames to distinguish representations.
See [format selection](docs/format-selection.md) and [native site coverage](docs/sites/README.md)
for the exact supported grammar, URLs, live evidence, and limitations.

Internet Archive, Wikimedia, Reddit hosted video, and SoundCloud audio have verified live media downloads. Vimeo
metadata, thumbnails and captions were verified; the sampled video was protected.
YouTube returned HTTP 429 and Bandcamp served a browser challenge during live
checks. Their independently implemented parsers have fixture tests, but live
playback is not claimed. No native path silently falls back to yt-dlp.

Native [collection selection](docs/collection-selection.md) preserves source
positions and rejects ambiguous end-relative requests on incomplete collections.
[Sidecar downloading](docs/assets.md) supports manual and automatic captions,
language/format preferences, thumbnails, and descriptions. Use `--list-subs` or
`--list-thumbnails` to inspect available assets without downloading. The default
subtitle preference is `vtt/srt/best`. Subtitle conversion and thumbnail embedding
are not implemented.

[Metadata embedding](docs/metadata.md) writes a verified `.embedded` sibling and
keeps the source and downloaded captions. Tags, chapters, and subtitle embedding
are independent flags. [Info JSON](docs/info-json.md) uses ownership receipts so
reruns can refresh tool-generated metadata while preserving user-edited files.
[HLS AES-128](docs/hls.md) handles ordinary identity keys; DRM and SAMPLE-AES remain
unsupported. Its initial manifest checks do not isolate later FFmpeg network reads.

The experimental YouTube signature compiler reproduces pinned research vectors
but is deliberately disconnected from downloading. Encrypted signatures, `n`
challenges, PO tokens, and SABR remain unsupported in native YouTube extraction.

## Configuration

Configuration is explicit, so another tool's config cannot silently affect downloads:

```toml
[omni]
directory = "downloads"
jobs = 4
media_type = ["image", "audio"]
retries = 3
timeout = 30
```

```sh
omni-dlp --config settings.toml 'https://example.org/gallery/'
```

Use argparse destination names for keys, as above. Command-line scalar settings override configuration. Repeatable settings append. Native archives use SQLite and are not interchangeable with yt-dlp's text archives. A verified archive hit takes precedence over `--force-overwrites`; omit the archive to force a fresh transfer.

## Python API

```python
from omni_dlp.engine import Downloader, Options
from omni_dlp.transfer import TransferOptions

client = Downloader(Options(
    transfer=TransferOptions(directory="downloads"),
    media_types={"image", "audio"},
    jobs=4,
))
items = client.inspect("https://example.org/gallery/")
results = client.fetch(items)
for result in results:
    print(result.path, result.sha256)
```

Installed extractor plugins use the `omni_dlp.extractors` entry-point group. An entry point exposes `supports(url)` and `discover(url, **kwargs)`, returning `Media` objects. Plugins are trusted Python code. `--no-plugins` disables loading them. See [architecture](docs/architecture.md).

## Research and verification

[Upstream research](docs/research/README.md) records a pinned source inventory and a timestamped snapshot of open issues. Inventory membership is not proof a site currently works. [Compatibility](docs/compatibility.md) explains the explicit upstream bridge.

```sh
python -m pip install -e '.[dev,compat]'
python -m pytest
ruff check .
python -m build
```

Tests use local HTTP fixtures and actual FFmpeg when available. They do not certify all live sites, platforms, protected streams, or upstream options. See [validation](docs/validation.md) for the checked release evidence.

MIT licensed. The optional yt-dlp and FFmpeg components retain their own licenses. This project is independent of yt-dlp.
