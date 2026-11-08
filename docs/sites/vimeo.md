# Vimeo native coverage

Native extractor key: `vimeo`. This implementation independently reads the
public player configuration. It does not import or run yt-dlp.

Supported URL forms:

- `https://vimeo.com/VIDEO_ID` and `/VIDEO_ID/UNLISTED_HASH`
- `https://player.vimeo.com/video/VIDEO_ID`, preserving an `h` query parameter
- Individual videos under `/channels/NAME/VIDEO_ID`,
  `/groups/NAME/videos/VIDEO_ID`, and `/album/ID/video/VIDEO_ID` or
  `/showcase/ID/video/VIDEO_ID`

These are individual video URL forms. Profiles, album/showcase enumeration,
reviews, Vimeo On Demand purchases, OTT, password submission, and account login
are not implemented.

The native extractor reads progressive files and HLS/DASH descriptors from the
player config endpoint, with an embedded-player-JSON fallback. It preserves
known dimensions/frame rates, metadata, thumbnails, captions, chapters, and
unlisted access hashes. Explicit `--media-type image` or `--media-type subtitle`
returns those assets independently. Selecting a specific HLS representation or
combining separated audio/video is the shared selection/manifest pipeline's job.

A manifest URL is **not** proof of clear media. Even a config field named `hls`
can lead to DRM-protected variants. Manifest candidates carry
`drm_status: unverified`; protected entries must be rejected during manifest
inspection or transfer. The extractor excludes explicitly protected progressive
entries and the `hls_cenc` family. It does not acquire DRM keys or decrypt protected
streams. JSON DASH indirection is resolved when provided, and errors in optional
manifest descriptors are recorded without discarding working progressive files.

Tests cover URL/hash handling, progressive/manifest schemas, caption/image assets,
metadata, malformed and restricted responses, identifier mismatches, non-HTTP
URLs, credential scope, and an actual local HTTP extraction/transfer round trip.
These fixtures establish schema handling, not universal current Vimeo access.

Live check on 2026-11-07 used Vimeo's public
[player demonstration](https://vimeo.com/76979871):

- Configuration and HLS master retrieval succeeded. The player exposed four
  video resolutions, external audio groups, and four caption languages.
- Tested default, AVC, and explicitly advertised fallback variants used
  SAMPLE-AES/FairPlay; DASH contained `ContentProtection`. Native video transfer
  failed safely. No completed live Vimeo video download is claimed.
- Native thumbnail download succeeded: 41,730 bytes, valid JPEG signature,
  SHA-256 `ea7908f06b4637041ffa2c98dd91cd6811b586fd5e6c8b381d5b77c7ab475aae`.
- Native caption download succeeded: 1,304 bytes, valid WebVTT signature,
  SHA-256 `94838f438fa8adfd71e013ae9c42b3181ed0ab1c9078f0df465200ee28f877d4`.

Signed CDN URLs and player credentials from the live response are not included
in the source tree or fixtures.
