# Native site coverage

These modules are independent code. Native operation never imports or executes
yt-dlp. Registration, fixture success, and live download success are different
levels of evidence. All seven families have partial coverage, not complete
upstream parity. The [migration ledger](../native-coverage.json) maps them to nine
upstream handler interfaces; other handlers remain unported.

| Family | Native surface | Live evidence on 2026-11-07 | Significant gaps |
| --- | --- | --- | --- |
| [Internet Archive](archive.md) | Item metadata, grouped AV alternatives, exact files, first-class non-video assets | MP3 and PDF downloaded and checked against source metadata | Restricted items, collections/search, all upstream access states |
| [Wikimedia](wikimedia.md) | File pages, originals, renditions, bounded categories | JPEG, Ogg, MP3 rendition, WebM and bounded category extraction | Every installation/permission state; full timed-text workflow |
| [Vimeo](vimeo.md) | Individual public/unlisted configurations, progressive and manifest descriptors, metadata/assets | Player metadata, JPEG and VTT downloaded; sampled video was DRM protected | Protected video, private login/password flows, collection enumeration |
| [Bandcamp](bandcamp.md) | Embedded track and album metadata, available audio and artwork | Browser challenge blocked live audio; fixtures and local HTTP round trips pass | Browser challenges, purchases/account access, complete discographies |
| [YouTube](youtube.md) | Individual videos with directly usable player formats, metadata, assets | HTTP 429 blocked live validation; player fixtures pass | Encrypted signatures, n challenges, PO tokens, SABR, playlists/channels |
| [Reddit](reddit.md) | Public post/gallery metadata, original images and animated variants, hosted HLS/DASH/progressive media, bounded crossposts | Hosted clip downloaded and merged with verified H.264 video/AAC audio; post JSON blocked by HTTP 403/login | Public post/gallery live coverage, OAuth/private posts, feeds/search, share redirects and external webpage delegation |
| [SoundCloud](soundcloud.md) | Public tracks, bounded sets, advertised progressive/clear HLS, artwork and metadata-only extraction | Public AAC HLS track downloaded; one-entry set retained the advertised total and incomplete status | Private/account access, previews, protected variants, profiles/search, original-upload downloads |

Selection and merging are shared across extractors. `-F` lists candidates, not
proof that the service will accept every transfer. A discovered manifest can
contain protected segments; the native backend rejects unsupported protection.
Use `--media-type image` or `--media-type subtitle` for the explicitly supported
asset modes. Shared [sidecar planning](../assets.md) also uses extractor-provided
subtitle/thumbnail/description metadata. Supported metadata-only paths retain
available public assets when main playback is unavailable; they do not bypass
access restrictions. Collection truncation and skipped records remain visible
in metadata. Collection selectors currently reject duplicate source positions,
including some animated-gallery and mixed artwork/audio records.

The [experimental YouTube signature compiler](../research/YOUTUBE-CHALLENGES.md)
passes isolated fixtures and three pinned public signature vectors. It remains
a disconnected research prototype because signature-role binding and indirect
JavaScript prototype semantics are unresolved. These checks do not establish
encrypted-signature playback. No `n` challenge, PO-token, SABR, or
protected-access support is implied.
