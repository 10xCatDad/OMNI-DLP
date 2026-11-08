# Wikimedia native extractor

Independent API extraction; no yt-dlp runtime imports or delegation.

## URL coverage

- `https://commons.wikimedia.org/wiki/File:Example.jpg`
- `https://commons.wikimedia.org/wiki/Category:Example_category`
- `File:` and `Image:` description pages on language editions of Wikipedia,
  Wiktionary, Wikibooks, Wikinews, Wikiquote, Wikisource, Wikiversity and Wikivoyage.
- Equivalent mobile hosts and `/w/index.php?title=File:...` URLs.
- File/category pages on `www.mediawiki.org`.

Host matching uses these Wikimedia-owned domain families, not arbitrary
MediaWiki installations. Ordinary article pages, upload histories, revision
selection, galleries embedded in article prose, and recursive subcategories are
outside this extractor's current coverage. Namespace prefixes in input URLs are
`File:`, `Image:`, and `Category:`; category member titles returned by the API may
use the wiki's localized namespace name.

## Extracted assets

File description pages yield the latest downloadable original with a stable
wiki/page identifier, media category, size, dimensions, duration where available,
and the source's SHA-1 metadata. The source SHA-1 is informational; the transfer
engine independently records SHA-256 of downloaded bytes.

Images and documents remain original files. Thumbnails and preview renders are
not substituted for originals. Audio/video resources expose an `original` format
and the renditions the site's TimedMediaHandler API currently returns. Rendition
IDs are prefixed with `transcode-`, such as `transcode-mp3` or
`transcode-360p.vp9.webm`. Unsafe identifier characters are sanitized with a hash
suffix, and the original transcode key stays in format metadata.

Select `original` when byte-for-byte preservation of the original upload is
required. A quality-based selection may choose a transcoded rendition. Width,
height, bitrate, declared codecs and file sizes are populated only when the API
supplies sufficient data; missing codec details are not invented. Returned timed
text links are stored in `metadata.subtitles`; this extractor does not itself
fetch or embed subtitle files.

The implementation uses the [TimedMediaHandler videoinfo
API](https://www.mediawiki.org/wiki/Extension:TimedMediaHandler/API), falling back
to [imageinfo](https://www.mediawiki.org/wiki/API:Imageinfo) when a project does
not provide TimedMediaHandler. Only one file revision is requested. Returned
original/rendition URLs must belong to Wikimedia's upload host. Credential
headers remain scoped to their original host and do not accompany cross-origin
upload requests.

## Categories and limits

Category extraction enumerates direct file members through the
[categorymembers API](https://www.mediawiki.org/wiki/API:Categorymembers), follows
its continuation token, and batches metadata requests in groups of at most 50.
The `max_items` limit bounds category members examined, including members later
excluded by media-type filtering. Redirect aliases are resolved, equivalent
files are deduplicated, and category ordering is preserved among output files.

Each returned item records:

- `playlist_title` and `playlist_index`.
- `collection_truncated` when the configured bound stops enumeration.
- `collection_skipped` for inaccessible or unusable member records.
- `collection_complete`, meaning enumeration reached its end without truncation
  or skipped records. It does not mean the files have been downloaded, or that a
  changing remote category was observed as an atomic snapshot.

Missing/private originals, malformed API results, cyclic redirects and stalled
pagination produce explicit errors. If some category members are unusable,
returned accessible items retain the non-complete metadata. If every member is
unusable, extraction fails rather than returning a successful empty collection.

## Validation

The fixture suite covers originals, audio/video renditions, Unicode/mobile URLs,
project fallback, credentials, host validation, hidden and malformed records,
category continuation, aliases, missing batches, limits and selector-safe IDs.

Live checks on 2026-11-07 used the independent extractor and native HTTP transfer:

| Source | Result | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| [Commons Example.jpg](https://commons.wikimedia.org/wiki/File:Example.jpg) | Original JPEG downloaded | 9,022 | `99bd915a3819b1fee3722328ae904dde5459e9b15d56abd98b903c770a2f582c` |
| [Commons Example.ogg](https://commons.wikimedia.org/wiki/File:Example.ogg) | Original Ogg downloaded | 104,793 | `f57b56d8aae4c847cf01224fb45293610d801cfdac43d932b5eeab1cd318182a` |
| [Commons Example.ogg](https://commons.wikimedia.org/wiki/File:Example.ogg) | MP3 rendition downloaded | 137,601 | `bfb379aec18bef3bbc5faa42667294f4335081c9dbf70f79fa789b111d931263` |
| [Commons Example.webm](https://commons.wikimedia.org/wiki/File:Example.webm) | Original video downloaded through native CLI and format selection | 235,171 | `f25d4512cb0051889cd8a65613159390c42485262e2c7b188cec58a40d198d3a` |

A live Commons category check examined two file members that both redirected to
one canonical file. It returned one deduplicated item and explicitly reported
truncation because further members remained. These checks establish those
specific public examples, not every wiki, category, media format, or access state.
