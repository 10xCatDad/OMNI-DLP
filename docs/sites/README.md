# Native site coverage

These modules are independent code. Native operation never imports or executes
yt-dlp. Registration, fixture success, and live download success are different
levels of evidence. All five families have partial coverage, not complete
upstream parity. The [migration ledger](../native-coverage.json) maps them to six
upstream handler interfaces; other handlers remain unported.

| Family | Native surface | Live evidence on 2026-11-07 | Significant gaps |
| --- | --- | --- | --- |
| [Internet Archive](archive.md) | Item metadata, grouped AV alternatives, exact files, first-class non-video assets | MP3 and PDF downloaded and checked against source metadata | Restricted items, collections/search, all upstream access states |
| [Wikimedia](wikimedia.md) | File pages, originals, renditions, bounded categories | JPEG, Ogg, MP3 rendition, WebM and bounded category extraction | Every installation/permission state; full timed-text workflow |
| [Vimeo](vimeo.md) | Individual public/unlisted configurations, progressive and manifest descriptors, metadata/assets | Player metadata, JPEG and VTT downloaded; sampled video was DRM protected | Protected video, private login/password flows, collection enumeration |
| [Bandcamp](bandcamp.md) | Embedded track and album metadata, available audio and artwork | Browser challenge blocked live audio; fixtures and local HTTP round trips pass | Browser challenges, purchases/account access, complete discographies |
| [YouTube](youtube.md) | Individual videos with directly usable player formats, metadata, assets | HTTP 429 blocked live validation; player fixtures pass | Encrypted signatures, n challenges, PO tokens, SABR, playlists/channels |

Selection and merging are shared across extractors. `-F` lists candidates, not
proof that the service will accept every transfer. A discovered manifest can
contain protected segments; the native backend rejects unsupported protection.
Use `--media-type image` or `--media-type subtitle` for the explicitly supported
asset modes. Collection truncation and skipped records remain visible in metadata.
