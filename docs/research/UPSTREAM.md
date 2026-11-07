# yt-dlp baseline and rewrite coverage

This is a compatibility inventory for an independent rewrite, not a declaration
that the rewrite is finished. Native omni-dlp does **not** implement yt-dlp's
site extractor library or its full command language. The explicit compatibility
command runs separately installed yt-dlp. Delegation preserves access to upstream
behavior, including upstream limitations, but is not independent parity.

## Evidence and scope

The source baseline is [yt-dlp commit
412e7f0a4b0588fe884a02b344ac66a06538475b](https://github.com/yt-dlp/yt-dlp/tree/412e7f0a4b0588fe884a02b344ac66a06538475b).
Capture time, commit time, input checksums, and source cleanliness are in
[upstream-summary.json](upstream-summary.json). The reproducible inventory uses
the actual parser and extractor registry rather than estimating from README
headings.

| Inventory | Records | Meaning |
| --- | ---: | --- |
| [CLI declarations](upstream-cli.json) | 323 | Includes 394 short/long spellings; 73 hidden declarations |
| [Registered extractor classes](upstream-extractors.json) | 1,754 | Includes redirect, error, protocol, and unsupported-site handlers |
| Documented site entries | 1,734 | Not a count of distinct websites or verified working services |
| Extractors explicitly marked broken | 136 | The remaining entries are not thereby live-verified |
| [Downloader classes](upstream-pipeline.json) | 22 | Includes base and external downloader classes |
| Explicit protocol dispatch entries | 17 | HTTP is the default and is listed separately |
| Registered postprocessor classes | 29 | Includes base classes |

The JSON files exhaustively enumerate these declared interfaces at the pinned
commit. They do not enumerate every implicit behavior, site permutation,
authentication state, external plugin, metadata-field interaction, historical
release, or unreported bug. An exhaustive list of every unsupported website or
possible future feature is not finite. [ISSUES.md](ISSUES.md) and
[upstream-issues.json](upstream-issues.json) provide a bounded, timestamped
inventory of open reported issues instead.

The 20 hidden extractor handlers explain the difference between registry and
documented site counts. Three of them deliberately recognize unsupported sites.
Their complete declared URL patterns are included under
`explicitly_unsupported_url_patterns` in the extractor inventory. These are
upstream classifications, not fresh tests of the services.

## Supported feature families upstream

The option inventory records every spelling, destination, parser action, value
type, and declared choice. The table below groups the behavior those options and
the source modules expose. Counts include hidden options in each group; aliases
are not separate features.

| Upstream surface | Declarations | Behavior to preserve |
| --- | ---: | --- |
| General | 35 | Error policy, extractor enumeration/selection, search defaults, configuration loading, compatibility settings, plugin discovery, cache controls, release updates, JavaScript runtimes and remote components |
| Network | 8 | Proxies, timeouts, source binding, IPv4/IPv6 selection, browser impersonation and target enumeration |
| Geo restriction | 6 | Verification proxy and forwarded-address/country/block controls; these do not grant account entitlements |
| Selection | 28 | Playlist slices and ordering, title/date/view/age filters, availability filtering, match expressions, download archives, count and break conditions |
| Download | 28 | Rate limits, retries and backoff, fragment concurrency, unavailable fragments, buffers/chunks, test downloads, section ranges, external downloaders, live waiting |
| Filesystem | 40 | Batch input, templates and paths, sanitization, overwrites, resume and partial files, timestamps, description/metadata/comments, archive semantics, cached data |
| Thumbnails | 4 | Individual/all thumbnail downloads and enumeration |
| Shortcuts | 4 | Desktop/platform URL shortcut sidecars |
| Verbosity and simulation | 33 | Quiet/logging, progress and terminal formatting, JSON/field output, simulation, metadata-only operation, diagnostics and request traffic |
| Workarounds | 12 | TLS policy, encodings, custom headers, sleeps, extraction workarounds |
| Formats | 21 | Selector grammar, sort grammar, multistream audio/video, format checks and enumeration, free-format preference |
| Subtitles | 8 | Manual/automatic captions, language filtering, format preference, enumeration |
| Authentication | 14 | Username/password and two-factor flows where supported, netrc, video password, TV-provider authentication, cookies from file/browser, client certificates |
| Postprocessing | 40 | Audio extraction, remux/transcode, subtitle/thumbnail conversion, metadata and thumbnail embedding, chapter splitting/removal, concatenation, fixups, xattrs, executable hooks |
| SponsorBlock | 5 | Segment queries, marking, removal, categories and API selection |
| Extractor configuration | 6 | Dynamic manifests, retries, extractor-specific arguments, generic extraction controls |
| Hidden root declarations | 31 | Deprecated/removed options retained for diagnostics or compatibility; declaration does not imply continued functionality |

Additional interfaces beyond CLI options:

- Embedded Python API, progress/postprocessing hooks, custom format selectors,
  custom extractors, and custom postprocessors.
- Per-site metadata, search, pagination, playlist/channel/feed traversal, embedded
  players, livestreams and replay handling, subtitles, thumbnails, and comments.
- HTTP, HLS, DASH, HDS/F4M, Smooth Streaming/ISM, RTMP, MHTML storyboards,
  websocket fragments, live-chat recording, and specialized site transports.
- External integrations for aria2c, axel, curl, FFmpeg, HTTPie, and wget.
- Native/optional HTTP backends, cookies and keyrings, TLS impersonation,
  compression, and websocket clients.
- Packaging, self-update channels, standalone builds, platform support, shell
  completions, documentation, and plugin installation conventions.

Primary references: [README at the pinned revision](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/README.md),
[options parser](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/options.py),
[downloader dispatch](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/downloader/__init__.py),
and [postprocessor registry](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/postprocessor/__init__.py).

## Upstream limitations and unsupported behavior

| Class | Evidence and interpretation | Rewrite consequence |
| --- | --- | --- |
| Declared broken sites | 136 extractor classes return `working() == False`; upstream explicitly says listed sites are not guaranteed to work | Track actual fixture/live evidence independently of registration |
| Unrecognized sites | Generic extraction may work without a dedicated extractor; failure is not proof that the media is unavailable | Use media-neutral discovery and explicit extraction errors |
| Arbitrary media collections | Upstream centers video/audio records and thumbnails; some images, storyboards, RSS enclosures and direct files already work | Add first-class image/document/archive entries without claiming yt-dlp has no image or podcast support |
| DRM | Upstream reports protected formats as unavailable and has a dedicated known-DRM classifier | No promise of DRM decryption or acquisition of protected keys |
| Account/private/geo content | Access depends on valid credentials, service policies, geographic availability, and extraction support | Preserve supported authentication; do not promise universal accessibility |
| Anti-bot/JavaScript changes | Browser impersonation and YouTube JavaScript challenge dependencies exist; service behavior remains volatile | Version extraction separately and keep reproducible fixtures |
| Removed features | README lists obsolete annotations, advertisement inclusion, `avconv` preference, legacy debug options and other removed spellings | Inventory removed declarations without counting them as functioning features |
| External downloader progress | README labels normalization of external-downloader output as not currently implemented | Design a native event contract; report opaque subprocess progress honestly |
| Platform/dependency limits | Baseline supports Python 3.11+; FFmpeg, JavaScript runtime/EJS, cookie keyrings and optional packages are task-dependent | Check capabilities at runtime and expose actionable dependency errors |
| No proof from an open issue alone | A report can be outdated, site-specific, duplicated, or require environment details | Classify issues as reports; validate reproducible failures before claiming a fix |

Sources: [declared site list](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/supportedsites.md),
[unsupported-site classifiers](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/extractor/unsupported.py),
[format and DRM contract](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/extractor/common.py),
and the pinned README's dependencies, removed options, and behavior sections.

## Native rewrite coverage

These are capability boundaries, not semantic equivalence with matching yt-dlp
flags. The native tests establish fixture-level behavior; they do not verify all
sites or every upstream option combination. Consult the root README and current
test results for the executable interface.

| Capability | Native implementation | Remaining upstream parity |
| --- | --- | --- |
| HTTP(S) file download | Independent transfer core, streaming, retries, rate/size controls, validator-aware partial transfer, atomic completion, SHA-256 | Full upstream retry/chunk/fragment semantics and network backend matrix |
| Media types | First-class images, audio, video, documents, archives and other directly addressable files | A file being downloadable does not imply decoding, conversion, preview or site extraction support |
| HTML discovery | Mixed image and AV elements, responsive images, embedded metadata and file links | Dynamic browser execution, arbitrary embedded player providers and all site APIs |
| Feeds and manifests | RSS/Atom enclosures and JSON manifests | Complete upstream playlist/search/pagination model and every feed dialect |
| HLS/DASH | FFmpeg transport integration | Independent streaming parsers, full selection, per-fragment journals, all encryption/live/subtitle variants |
| Request context | Custom headers and cookie files | Browser-cookie/keyring extraction, all auth flows, impersonation, client certificates and all proxies |
| Selection | Media-kind filters and native item limits | Upstream expression grammar, date/view/age filters, playlist range and ordering equivalence |
| Output management | Native templates, paths, metadata/manifests, integrity hashes, archive and concurrent jobs | Every upstream template conversion, path type, overwrite and archive edge case |
| Postprocessing | FFmpeg audio extraction and remux operations | SponsorBlock, multistream merge logic, metadata/subtitle/thumbnail embedding, chapter edits, all converters/fixups |
| Command/API | Native CLI, batch inputs, TOML settings, inspection/simulation and plugin API | Drop-in CLI/Python API and plugin compatibility |
| Site library | Generic discovery only | All 1,754 upstream registered handlers remain unported |
| Compatibility command | Explicit delegation to installed yt-dlp | This is upstream execution, not an independent rewrite |

## Implementation priorities derived from the inventory

1. Make all media types equal at the record/transfer layer. Avoid a video-only
   schema that makes images and documents thumbnail side effects.
2. Establish integrity and recovery behavior before expanding site count:
   truncated responses, changed validators, ignored ranges, retries, collisions,
   interrupted writes, archive updates, and concurrent work need adversarial
   offline tests.
3. Separate discovery, selection, transfer, processing, and persistence. Preserve
   provenance without leaking credentials into logs or manifests.
4. Add a fixture and capability declaration for each independently ported site.
   A URL regex or compatibility fallback does not count as a port.
5. Port upstream grammars and processing behavior against differential tests;
   record intentionally changed defaults and unsupported combinations.
6. Expand streaming support, authentication, plugins and platform builds only
   after their recovery/security semantics have executable evidence.
7. Treat upstream issue requests as a ranked backlog, not an automatic promise.
   Mark a request resolved only when the relevant behavior and regression test
   exist in this project.

## Refreshing the inventory

```sh
git clone --depth 1 https://github.com/yt-dlp/yt-dlp.git upstream-yt-dlp
python scripts/inventory_upstream.py upstream-yt-dlp --output docs/research
python scripts/snapshot_issues.py --help
```

The source inventory imports the selected checkout, so use trusted source in a
fresh interpreter. It does not invoke downloads, load upstream user config, or
write to upstream. Issue collection is read-only and its own provenance records
whether pagination completed. A newer source checkout or refreshed issue list
is a new baseline and must not silently replace the meaning of an old result.
