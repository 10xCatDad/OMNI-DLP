# Validation evidence

Validation date: 2026-11-07. This evidence covers the native core and the collection,
asset, metadata, HLS, and extractor expansion in **0.3.0a1**. It does
not establish complete yt-dlp parity or verify all upstream sites.

## Local integration tests

The 0.3.0a1 local suite passes 1,316 tests plus 45 unittest subtests on Python 3.12.
Ruff, whitespace checks, and wheel/source builds pass. A clean wheel installation
without yt-dlp verified a native image download with caption, artwork and
description attachments, a fully verified archive repeat, and CLI info-JSON
ownership updates. This checks installed package behavior, not only editable code.

The previous 0.2.0a1 suite passed 537 tests plus 29 subtests. Its successful matrix
is historical evidence. The current release records its own final-commit CI
results; earlier results do not establish new-code coverage.

A real CLI SIGINT subprocess test verifies prompt exit 130 during an active
transfer and retention of a resumable partial. Separate FFmpeg subprocess tests
verify cancellation during adaptive transfer and conversion. Stalled network
I/O can still take the configured socket timeout to return.

The offline suite exercises actual local HTTP servers, redirect/authentication
boundaries, mixed pages, RSS/Atom/JSON feeds, MIME and arbitrary binary resources,
byte integrity, truncation, range/validator edge cases, transient retries,
filesystem collisions, disk-space preflight, SQLite archives, plugin contracts,
CLI/configuration behavior, and optional compatibility forwarding.

When FFmpeg is installed, tests generate real HLS and DASH fixtures, serve them
over HTTP, remux them, inspect stream/container metadata with ffprobe, convert
audio to AIFF/M4B/FLAC, and check incomplete streams, local-file references,
output tampering, locking and stale-conversion recovery. These are actual media
operations, not just mocks of command construction.

The CI matrix runs Python 3.11 and 3.14 on Linux, Windows, and macOS. FFmpeg is
explicitly installed on Linux. Media tests skip on a runner without FFmpeg, so a
passing job with skipped tests must not be described as full media validation.

## Native extractor and selection expansion

The native format parser has fixed differential cases and a seeded matrix of
864 comparisons against the optional installed upstream package. These cover the
documented subset of grammar and ranking, not all upstream combinations. Native
runtime paths do not import that package. Tests also download and merge actual
separate video, audio and subtitle fixtures using FFmpeg.

Integration regressions cover signed-URL renewal, per-format identities, different
merge containers in archives, archived output/manifest collisions, redirected
manifest bases, target-scoped headers and continuing after selection failures.

The new collection suite includes 600 seeded upstream playlist comparisons and
size-filter boundary comparisons. Local HTTP tests verify native caption and
thumbnail bytes, sidecar-only operation, failed-asset retries, credential scoping,
archive recovery into older directories, and collection-wide collisions involving
actual archived outputs. Metadata-only records are rejected by both ordinary
batch and single-item download APIs when no playable format exists.

Real FFmpeg fixtures verify ordinary AES-128 HLS decryption, implicit/explicit
IVs, key rotation, invalid/missing keys, unsafe key protocols, and error cleanup.
Separate embedding tests inspect tags in MP4/MKV/MP3/FLAC/Ogg/Opus/WAV/AIFF,
chapter timing, preserved source streams, newly added subtitle languages/codecs,
size limits and cancellation. Info-JSON tests verify ownership, user-edit
preservation, receipt corruption, locks, bounded reads, atomic updates and
interrupted-publication failure. These are bounded fixtures, not exhaustive
codec, filesystem, concurrent-adversary or live-stream qualification.

## Experimental YouTube compiler

The disconnected research module has 142 offline tests. The explicit corpus
verifier independently derived transformations for captured players `74edf1a3`
and `901741ab` and passed all three pinned public signature vectors. No downloaded
JavaScript was executed. A separate review also compared accepted authored
programs against Node, but the normal suite and native runtime require no Node.

These checks do not prove signature-role association or whole-program JavaScript
semantics. The compiler is not called by the extractor. Encrypted signature
deciphering remains unsupported for native downloads; see the
[research boundary](research/YOUTUBE-CHALLENGES.md).

## Live extraction evidence

Live checks and exact limitations are recorded [per extractor](sites/README.md).
The complete native CLI additionally downloaded an Internet Archive MP3 and
Wikimedia JPEG/WebM using the new registry and selection pipeline. Their hashes
match the file records in the corresponding site documents. YouTube and Bandcamp
live playback remain unverified due HTTP 429/browser challenges. Vimeo assets and
metadata work in the tested example; its sampled video was DRM protected.
This round additionally downloaded and merged a public Reddit hosted video and
downloaded/remuxed SoundCloud audio. Their site documents record exact durations,
stream types, sizes and hashes. SoundCloud set enumeration was tested only within
the stated one-entry bound. Reddit public-post JSON remained unavailable from
this network; its post and gallery parsers have fixture coverage.

## Initial direct-download smoke checks

| Input | Native result | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| [W3C PNG](https://www.w3.org/Icons/w3c_home.png) | Followed redirect and downloaded image | 1,848 | `590608bf256237f22d84bbab2a0a1afc0d4f20db6350bbac2c6841f1f3e13b01` |
| [Wikimedia Ogg](https://upload.wikimedia.org/wikipedia/commons/c/c8/Example.ogg) | Downloaded audio | 104,793 | `f57b56d8aae4c847cf01224fb45293610d801cfdac43d932b5eeab1cd318182a` |
| [W3C Sintel trailer](https://media.w3.org/2010/05/sintel/trailer.mp4) | Downloaded video | 4,372,373 | `b670602fa00934ca27c4351bb0efe7ea7a07fae57284e44226025eeed7c51254` |

The same Ogg was downloaded through `omni-dlp compat` using yt-dlp 2026.10.26.
Its SHA-256 exactly matched the native download. This checks an actual delegated
download, not just executable availability, and does not certify other sites.
Live resources can change after the recorded checks.

## Remaining verification

- No authenticated proprietary site extraction has been independently ported or
  live-validated.
- No multi-hour live stream, very large download, power-loss recovery, or hostile
  concurrent filesystem mutation claim is made.
- Image MIME/extension classification is tested; magic-byte image validation is
  pending.
- CI platform results and exact test totals are recorded with the release. A
  workflow definition alone is not evidence that its jobs passed.
