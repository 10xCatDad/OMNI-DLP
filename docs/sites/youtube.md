# Native YouTube extraction

Status: **partial native extractor, not full YouTube support**. This module parses
YouTube player responses independently. It neither imports nor invokes yt-dlp.
A supported URL is not a guarantee that the current service provides formats the
native implementation can download.

## Supported URL and response shapes

- Single-video `watch?v=`, `youtu.be`, `/embed/`, `/shorts/`, `/live/`, and `/v/`
  URLs on the recognized YouTube, mobile, music, and privacy-enhanced hosts.
- Embedded `ytInitialPlayerResponse` assignments, JSON script blocks, JSON.parse
  of a JSON string, and legacy `ytplayer.config.args.player_response` data.
- A response must match the requested video ID. Unrelated recommended videos
  cannot become the download target.
- Direct HTTP(S) progressive/audio/video representations with dimensions,
  frame rate, bitrate, size, codec and audio-language metadata.
- Already-clear `sig`/`signature` values in cipher-encoded query data. This is
  query decoding, not encrypted-signature deciphering.
- Clear HLS/DASH manifest references. Format discovery inside a manifest is a
  separate pipeline capability; this extractor exposes the manifest itself.
- Title, description, uploader, channel ID, duration, views, upload date,
  thumbnails, manual captions, automatic captions, and player-overlay chapters.

`--media-type audio` requests audio-only representations. Video extraction keeps
separate audio candidates so format selection can combine them with video.
Explicit `image` and `subtitle` filters expose thumbnails and caption URLs as
first-class assets. By default they remain metadata rather than extra downloads.
Caption URLs request WebVTT. Downloading a listed caption still depends on the
service accepting the request; authentication and proof-of-origin may be needed.

## Known gaps

Encrypted `s` signatures, `n` throttling challenges, generated proof-of-origin
(PO) tokens, and SABR streaming are not implemented. Candidates with encrypted
signatures, unsolved `n` values, DRM indicators, invalid protocols, or missing
URLs are omitted with reason counts. If no usable representation remains, the
extractor fails explicitly rather than downloading the watch-page HTML.

A direct URL can still expire or require server-side checks that are not visible
in the response. Merely returning a URL is not evidence that its bytes can be
retrieved. DRM decryption, authentication/age-verification workflows, channel
and playlist pagination, search, comments, clip resolution, YouTube Music albums,
full subtitle translation, and chapter extraction from descriptions are pending.
No remote JavaScript is evaluated.

The current upstream implementation uses separately versioned JavaScript
challenge providers and per-client proof-of-origin policies. Replacing those
requires an independently implemented solver, isolated execution or a restricted
interpreter, current player fixtures, and live validation. A hardcoded historical
reverse/slice/swap parser would not establish current solver parity. Relevant
primary code references at the research baseline:

- [YouTube video extraction and format access checks](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/extractor/youtube/_video.py)
- [JavaScript challenge-provider implementation](https://github.com/yt-dlp/yt-dlp/blob/412e7f0a4b0588fe884a02b344ac66a06538475b/yt_dlp/extractor/youtube/jsc/_builtin/ejs.py)

These references informed the requirements and boundaries. Their implementation
is not a runtime dependency of the native extractor.

## Evidence

`tests/test_youtube.py` uses synthetic responses with no live signed URLs or
account material. It verifies routing, identity matching, malformed data,
format/codec metadata, access denial, omissions, captions, thumbnails, chapters,
header isolation and an actual local HTTP extraction/download with byte equality.
No downloaded or remote script is executed during these tests.

On 2026-11-07, an unauthenticated live request for the public
[downloader test video](https://www.youtube.com/watch?v=BaW_jenozKc) returned
HTTP 429 from this environment. An independent web fetch of its embed page was
also throttled. Live extraction and playback are therefore **not verified**.
The rate-limited request was not repeatedly retried. Fixture success must not be
presented as live YouTube compatibility.
