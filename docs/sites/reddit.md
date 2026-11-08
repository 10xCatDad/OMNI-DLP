# Reddit native extractor

The `reddit` extractor independently reads public post JSON and Reddit-hosted
media. It does not import or execute yt-dlp. Public post listings are described
in [Reddit's API documentation](https://www.reddit.com/dev/api/#GET_comments_{article}).
Actual access depends on Reddit's current public endpoints and network policy.

## URL coverage

- Reddit post URLs under `/comments/ID`, `/r/SUBREDDIT/comments/ID`, or
  `/user/USER/comments/ID`, including `.json` and title suffixes.
- `/gallery/ID` pages and `https://redd.it/ID` short post URLs.
- The standard, `www`, `old`, `new`, mobile, and other single-label
  `reddit.com`/`redditmedia.com` subdomains with those post paths.
- Direct `https://i.redd.it/FILENAME` assets, including animated MP4 files.
- `https://v.redd.it/VIDEO_ID` and direct recognized media resources below it.

Search, subreddit feeds, user histories, comments as separate entries, share
redirects under `/s/`, OAuth login, private posts, account preferences, and
external webpage delegation are not implemented. External links to recognizable
direct audio, image, video, document, or other supported files can be extracted
from an accessible post. A recognized URL is not a promise that its content is
publicly accessible.

## Media and metadata

Post video data supplies HLS, DASH, and progressive fallback choices. Reddit's
progressive video fallback is represented as video-only; HLS expansion can
select separate video and audio and merge them. A bare hosted-video URL probes
its public HLS and DASH manifests but has no post title, author, or description.
Older public post metadata containing only a hosted-video URL can use those
same probes. Ordinary gallery images use original resource URLs, not previews.
An animated gallery entry exposes its original GIF and silent MP4 as distinct
assets. Embedded post metadata can also provide audio and video resources.

Gallery ordering, original gallery positions, captions, dimensions, media type,
author, subreddit, timestamps, description, thumbnail candidates, and available
caption links are preserved. Caption language remains `und` when the source
does not identify it. Embedded crossposts are inspected to depth four, with at
most twenty distinct post records and four hosted-video fallback probes.
Each gallery or embedded media mapping is bounded to 5,000 entries. Requested
media-type filtering precedes the `max_items` limit.

Unavailable, malformed, and limited entries set collection diagnostics rather
than claiming a complete gallery. `playlist_index` and `playlist_count` preserve
the original positions even if some entries are unavailable. GIF and MP4
renditions share a gallery position but have distinct archive identities.
Format IDs are selector-safe, and identities do not depend on signed query
parameters. Only HTTP(S) child URLs are accepted. Caller credentials remain
scoped to their original origin, including after manifest redirects.

Metadata-only extraction can retain a valid public text/link post for JSON or
description sidecars without a main downloadable resource. Its required-format
marker prevents the webpage from being downloaded as media in a normal run.
Unavailable or private post metadata still fails explicitly.

## Verification

Offline fixtures cover accepted and rejected URL shapes, original gallery
ordering, mixed media, GIF/MP4 variants, private and malformed entries, embedded
crossposts, bounds, alternate video formats, thumbnails, subtitle language,
metadata-only records, invalid numeric values, safe child URLs, stable IDs,
source headers, and final redirect origins.

Live checks on 2026-11-07 used the public hosted clip
[`zv89llsvexdz`](https://v.redd.it/zv89llsvexdz). Both HLS and DASH manifests
were accessible. Native HLS parsing exposed video renditions and separate audio
tracks, and selected a 640-pixel-high video plus AAC audio. The corresponding
public post JSON request returned HTTP 403; the old-site endpoint redirected
to a login page. Therefore post/gallery parsing has fixture coverage, with
live public-post extraction unverified from this network. No login, opt-in
cookie, or account-access workaround was used.

The native downloader then fetched and merged the selected HLS components into
a 2,364,838-byte Matroska file. FFprobe verified H.264 video at 352 × 640,
AAC audio, and an 11.842-second duration. The SHA-256 of that output was
`23a3deb7846070bbcf7ebbbb9160d63aed177b61f9bf387b7022674e53d1ad5e`.
This demonstrates extraction, adaptive selection, transfer, and merging for
that public hosted clip at the recorded time; it does not establish every
Reddit post, gallery, caption, or restricted-access state.
