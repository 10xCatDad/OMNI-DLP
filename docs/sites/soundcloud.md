# SoundCloud native extractor

Independent extraction from public website metadata, with no yt-dlp runtime
imports or delegation.

## URL coverage

- Public tracks: `https://soundcloud.com/artist/track`.
- Public sets/albums: `https://soundcloud.com/artist/sets/collection`.
- Equivalent `www.soundcloud.com` and `m.soundcloud.com` URLs.
- `https://on.soundcloud.com/code` links that resolve to a supported public page.

Only HTTP(S) URLs on these hosts are accepted. The final redirected page is
validated before its metadata is used. Private-link tokens and account-token
query parameters are rejected. Profiles, search results, likes, repost feeds,
embedded players, account libraries, private tracks and private sets are outside
this extractor's current coverage.

## Assets and metadata

Public tracks expose advertised progressive HTTP and clear HLS audio renditions.
Format IDs such as `sc-progressive-mp3_128k` and `sc-hls-aac_160k` are usable by
the format selector. Unusual preset names are sanitized with a hash suffix;
their original values remain in format metadata. Formats carry declared codecs,
bitrate when present in the preset, and an explicit audio-only marker.

Track metadata includes stable `archive_id` values (`soundcloud:<track-id>`),
title, duration, uploader, description, genre, license, timestamps and artwork
when supplied by the page/API. Artwork URLs populate `metadata.thumbnails`.
Explicit image selection returns the advertised artwork, or uploader avatar
fallback, as an independent image with identity `soundcloud:<track-id>:artwork`.
It does not claim that the advertised image is the original artwork upload.
The extractor does not supply lyrics, subtitles, waveform data or comments.

Metadata-only extraction can return public track details without a playback
client or playable audio. Its audio record has no formats and is marked
`formats_required=True`, preventing the webpage placeholder from being mistaken
for a downloadable audio file. Normal audio extraction still requires a full
playable rendition. Private sharing flags and secret tokens are rejected in
both modes.

## Public website configuration

The extractor reads the public web application's advertised client identifier
from page hydration or inline configuration. If absent, it examines at most six
advertised JavaScript assets on `a-v2.sndcdn.com` without executing JavaScript.
It keeps that identifier in memory for the extraction and does not include it
in the returned media metadata. There is no embedded real client identifier,
client secret, account login, or OAuth token exchange.

The website playback endpoints on `api-v2.soundcloud.com` are distinct from
SoundCloud's documented developer API. The official [API
guide](https://developers.soundcloud.com/docs/api/guide) describes registered
applications and OAuth authorization for `api.soundcloud.com`. This extractor
does not implement that developer API or promise stability of the website's
internal response schema.

Playback URLs must use SoundCloud's `sndcdn.com` or `soundcloud.cloud` storage
domain families. Artwork must use `sndcdn.com`. Custom credential headers remain
scoped to the original input origin; they are not forwarded to cross-origin
API, script, or media requests. Cookies follow the shared client's normal cookie
domain rules.

## Sets, limits and failures

Sets retain the source order, `playlist_id`, `playlist_title`, one-based
`playlist_index` and the advertised `playlist_count`. The extractor refreshes an
incomplete embedded listing once when needed. Selected track-ID stubs are
resolved in batches of at most 50. Repeated IDs are deduplicated.

`max_items` bounds both the number of source track entries examined and total
returned audio/artwork assets. Each track resolves at most 16 advertised
transcodings. Collection metadata includes `limited`, `collection_complete`,
`unavailable_track_ids` and `extraction_warnings`. A skipped, missing, restricted,
or malformed track prevents a complete-collection claim. Completion describes
enumeration, not successful file downloads or an atomic snapshot of a changing
set. A nonempty set with no playable selected tracks fails normal audio
extraction rather than appearing to be an empty successful download.

Preview-only tracks/variants, unsupported protected playback protocols,
subscription or regional restrictions, original-upload download endpoints, and
account-only media are not handled. An individual unavailable rendition is
recorded in `format_warnings` when another full rendition remains usable.
Expired or withdrawn playback URLs may require extracting the public page again.
HTTP, malformed-response and missing-public-client failures remain explicit.

## Validation

The fixture suite covers public and rejected URL forms, progressive/HLS
renditions, selector-safe IDs, malformed responses, scoped headers, client
discovery without JavaScript execution, short-link final URLs, previews,
restricted/private tracks, bounded sets, missing batches, collection diagnostics,
artwork and metadata-only extraction.

Live checks on 2026-11-07 used the independent extractor and native downloader:

- [Pegboard Nerds - Disconnected (Original
  Mix)](https://soundcloud.com/monstercat/pegboard-nerds-disconnected) exposed two
  AAC HLS renditions. The selected `sc-hls-aac_160k` downloaded and remuxed to a
  4,914,027-byte Matroska file. `ffprobe` reported one AAC audio stream, no video,
  and 241.921 seconds. SHA-256:
  `ba3adba3cb3cae3fecdceb485b31af7ccfe29fac9557bd37e639d76f0f59d683`.
  A third advertised playback endpoint returned HTTP 404 and was recorded as a
  format warning. The checksum describes that local remux, not the source upload.
- [Monstercat
  Instinct](https://soundcloud.com/monstercat/sets/monstercat-instinct), bounded to
  one source entry, returned track `2432443878` with two AAC HLS formats and
  `playlist_index=1`. The service reported 474 tracks; the result correctly had
  `limited=True` and `collection_complete=False`. The remaining set was not
  downloaded or validated.
- [The Royal Concept
  EP](https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep), also
  bounded to its first entry, advertised six unsupported/protected playback
  variants for track `75206121`. Normal audio extraction failed explicitly.

These checks establish the specific public examples above, not every track,
region, playback protocol, rendition or SoundCloud access state.
