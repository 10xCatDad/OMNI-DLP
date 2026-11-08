# Bandcamp native coverage

Native extractor key: `bandcamp`. This implementation does not import or run
yt-dlp. It parses the public player's `data-tralbum` JSON attribute.

Supported URL forms are `https://ARTIST.bandcamp.com/track/SLUG` and
`https://ARTIST.bandcamp.com/album/SLUG`, including optional query strings and a
trailing slash. Custom artist domains, artist discographies, radio, purchases,
and account libraries are not implemented.

A track becomes one audio item with the public `file` alternatives exposed as
formats. Albums preserve track order, stable track IDs, track numbers, album
metadata, and available cover metadata. Only explicitly offered stream URLs are
used. This does not promise purchase-quality FLAC or other paid download options.
`--media-type image` selects available cover artwork as an independent image.

`max_items` bounds returned items. Album records include `playlist_count`,
`limited`, `collection_complete`, and `unavailable_track_ids`. These describe
extraction coverage, not successful download completion. An album with some
unplayable tracks can return its available tracks with incomplete coverage marked.
An album with no public audio raises an actionable error. Track pages cannot
silently select a different track from a multi-entry player response.

Fixture tests exercise track/album data, multiple formats, ordered collections,
missing streams, bounds, covers, malformed JSON, non-HTTP URLs, foreign-origin
credential exclusion, and an actual local HTTP extraction/transfer round trip.

Live check on 2026-11-07: the public [FTL album page](https://benprunty.bandcamp.com/album/ftl)
was visible through the web research service, but native HTTP received a
JavaScript client challenge. Native extraction reports that limitation rather
than downloading the challenge HTML as audio. Live Bandcamp audio downloading
has **not** been verified. Browser challenge completion, browser impersonation,
and authenticated purchase downloads remain migration work.
