# Upstream issue snapshot and rewrite priorities

The [machine-readable inventory](upstream-issues.json) contains **2,074 open
issues** from [yt-dlp/yt-dlp](https://github.com/yt-dlp/yt-dlp/issues), collected
on 2026-11-07 between 23:21:16 and 23:21:54 UTC. The traversal reached the end
after 28 pages, with 2,771 unique issue-or-PR records, no duplicate records,
697 pull requests excluded, and no request failures. GitHub search separately
reported 2,074 open issues and 697 open PRs with `incomplete_results=false`.

There is a count discrepancy worth preserving: repository metadata reported
2,773 open issues plus PRs before and after collection, two more than either
the complete issues endpoint traversal or search. Its cause is unverified.
The snapshot is complete for the rows returned by the endpoint, not an atomic
or provably exhaustive view of every internally counted GitHub record. Public
issue state can change during pagination. Closed issues, discussions, comments,
bodies, and PR implementation details are outside this snapshot's scope.

Each saved issue retains only its number, title, URL, labels, state, and creation
and update timestamps. The script does not persist reporter identity or issue
bodies. Labels are upstream triage metadata, not independent confirmation of a
bug or a definitive list of unsupported functionality.

## Current triage surface

| Upstream label | Open issues carrying label | Interpretation |
| --- | ---: | --- |
| `site-bug` | 834 | Reported problems with site behavior; not 834 unsupported sites |
| `site-request` | 430 | Requested site coverage; generic extraction may already work partially |
| `site-enhancement` | 302 | Requested extensions to existing site behavior |
| `enhancement` | 266 | Core or cross-cutting changes, including some reported defects |
| `bug` | 175 | General bug reports, not all reproduced here |
| `account-needed` | 249 | Testing may require authorized account access |
| `geo-blocked` | 143 | Location-dependent testing constraints |
| `patch-available` | 116 | A potential patch exists; this does not establish that it works |
| `external-issue` | 65 | Upstream believes another component contributes to the problem |
| `DRM` | 31 | Content protection constraints require separate treatment |

Labels overlap, so these rows must not be added to calculate a total. The JSON
contains every observed label and count. Most issues also retain a triage label;
an open report alone is insufficient evidence that current code is broken.

## Feasible requests that inform the design

The following are implementation candidates and acceptance criteria, not claims
that omni-dlp has resolved each upstream issue. Its current capability matrix
and tests are the authority for implementation status.

| Request or report | Classification | Rewrite implication and acceptance criterion |
| --- | --- | --- |
| [#15295](https://github.com/yt-dlp/yt-dlp/issues/15295): AIFF as an audio output | Additional output choice | Expose an explicit AIFF conversion profile; verify codec/container with ffprobe. Cover art is a separate requirement and must not be implied by conversion alone. |
| [#11761](https://github.com/yt-dlp/yt-dlp/issues/11761): M4B as an audio output | Additional output choice | Use an audiobook-compatible container profile and correct extension; separately test chapter preservation. This is not evidence that upstream cannot already process audiobook audio. |
| [#13649](https://github.com/yt-dlp/yt-dlp/issues/13649): unbuffered structured progress | Requested interface improvement | Emit versioned JSON events with immediate flushing when piped; test from a non-TTY subprocess. Existing upstream progress templates must not be described as absent. |
| [#14265](https://github.com/yt-dlp/yt-dlp/issues/14265): item/total progress | Requested interface or documentation improvement | Separate discovery, item index, transfer, and postprocessing events; represent unknown totals explicitly. |
| [#15714](https://github.com/yt-dlp/yt-dlp/issues/15714): detect insufficient disk space | Requested reliability improvement | Preflight known byte counts when possible, retain partial data on ENOSPC, and never emit completed success after failed writes. This report includes a GUI environment and is not independently reproduced here. |
| [#15242](https://github.com/yt-dlp/yt-dlp/issues/15242): retry thumbnail downloads | Reported inconsistency in an existing feature | Use the same retry and integrity policy for images and other downloaded assets; inject transient failures in tests. |
| [#14542](https://github.com/yt-dlp/yt-dlp/issues/14542): disable redirects per request | Requested networking control | Make redirect policy explicit and bounded; test reading a 302 response without automatically following it. |
| [#14144](https://github.com/yt-dlp/yt-dlp/issues/14144): retain resumable large partial files | Requested recovery policy | Preserve existing partial data until a replacement transfer is safe. Never force append after an invalid range response or changed resource validator. |
| [#12994](https://github.com/yt-dlp/yt-dlp/issues/12994): HTTP 416 resume failure | Reported downloader bug | Test already-complete partial files, server shrinkage, changed validators, and malformed Content-Range. A 416 alone is not proof that a partial file is complete. |
| [#16387](https://github.com/yt-dlp/yt-dlp/issues/16387): thumbnail extension disagrees with content | Reported defect in existing thumbnail processing | Detect byte signatures and reconcile MIME/extension before conversion; test GIF content served as `.jpg`. |
| [#11627](https://github.com/yt-dlp/yt-dlp/issues/11627): refresh archive state per download | Requested archive behavior | Query persistent state at scheduling/commit boundaries; define how concurrent workers handle duplicate jobs. |
| [#11965](https://github.com/yt-dlp/yt-dlp/issues/11965): archive whole playlists | Requested additional archive granularity | Distinguish resource completion from collection completion. Only mark a collection complete after every required item succeeds. |
| [#5485](https://github.com/yt-dlp/yt-dlp/issues/5485): increment duplicate names | Requested naming policy | Make collisions explicit; stable identifiers or safe numbering must avoid silently overwriting unrelated files. |
| [#17328](https://github.com/yt-dlp/yt-dlp/issues/17328): retain truncated downloads | Requested partial-success behavior | If implemented, require an explicit opt-in and label the output incomplete. Do not conflate incomplete fragments with a successful complete download. |

## Media-neutral additions

The main architectural addition is treating each resource as a typed asset,
rather than assuming that a downloadable item is a video with optional sidecars.
That permits image galleries, podcast attachments, document links, archives,
fonts, and other common file formats to share retry, resume, integrity, archive,
and provenance behavior. Support for opaque file transfer does not imply support
for decoding, conversion, or specialized extraction for every such format.

Related upstream reports illustrate important distinctions:

- [#10870](https://github.com/yt-dlp/yt-dlp/issues/10870) and
  [#15764](https://github.com/yt-dlp/yt-dlp/issues/15764) concern TikTok photo posts
  and their audio. A generic image extractor does not solve TikTok authentication,
  post parsing, or audio extraction automatically.
- [#15209](https://github.com/yt-dlp/yt-dlp/issues/15209) reports a Reddit gallery
  extraction loop. This is a site-specific failure report, not proof that every
  image gallery is unsupported.
- [#5222](https://github.com/yt-dlp/yt-dlp/issues/5222) requests Google Photos;
  [#9702](https://github.com/yt-dlp/yt-dlp/issues/9702) requests shared Synology
  Photos coverage. Both need dedicated endpoint/access tests beyond downloading
  a direct image URL.
- [#11560](https://github.com/yt-dlp/yt-dlp/issues/11560) requests complete Apple
  Podcasts collections, while [#17992](https://github.com/yt-dlp/yt-dlp/issues/17992)
  reports an Art19 RSS failure. Generic RSS enclosure support helps the common
  transport layer but does not establish site-specific collection parity.
- [#12294](https://github.com/yt-dlp/yt-dlp/issues/12294) mentions image-only
  downloads in its title but describes a video unexpectedly yielding only a
  thumbnail. It must not be counted as a general request for image downloading.

No finite issue list describes all unsupported behavior. The complete option
and extractor inventories capture declared upstream surfaces. An independent
rewrite still needs tests for each claimed behavior, authenticated service
fixtures where permitted, and live checks for services whose APIs change.

## Refresh

```sh
python scripts/snapshot_issues.py
# Optional: use an explicitly selected, account-scoped GitHub CLI executable.
python scripts/snapshot_issues.py --gh /path/to/scoped-gh
```

The default is unauthenticated public HTTPS. A partial run writes its coverage
and error state and exits nonzero; missing data is never reported as zero issues.
