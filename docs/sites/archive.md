# Internet Archive native extractor

The `internetarchive` extractor independently reads the public item metadata API.
It does not import or execute yt-dlp. The API contract is documented by
[Internet Archive](https://archive.org/developers/md-read.html).

## URL coverage

- `https://archive.org/details/IDENTIFIER`
- `https://archive.org/embed/IDENTIFIER`
- `https://archive.org/download/IDENTIFIER`
- An exact file below `/download/IDENTIFIER/FILENAME` or
  `/details/IDENTIFIER/FILENAME`, including nested and URL-encoded filenames.
- The `www.archive.org` alias and HTTP URLs are recognized; API/file requests
  use HTTPS.

This does not include Wayback Machine capture URLs, collection traversal, search
results, BookReader page/mode suffixes, files inside uploaded ZIP/TAR containers,
lending/entitlement flows, or upload/account management. Recognized item
URLs can still fail because an item was deleted, restricted, or unavailable.

## Media representation

An item may contain audio, video, images, documents, archives, subtitles, or
other files. Images and documents are downloadable assets in their own right.
For item-level discovery, generated records whose source or format explicitly
identifies them as administrative metadata are omitted. Request an exact file
URL to download such a file explicitly.

Same-kind audio/video derivatives are grouped through the API's `original`
relationship. A FLAC track and its MP3/Ogg derivatives become one `Media` with
three format choices. A waveform PNG remains a separate image, and PDF/EPUB/
OCR/document variants remain separate assets. Simple filenames are retained as format identifiers. Filenames containing
selector punctuation, whitespace, non-ASCII characters, or reserved selector
names receive deterministic selector-safe identifiers; their original filename
remains in each format's metadata. Available size, duration, dimensions, bitrate and codec hints
are preserved; missing information stays unknown. Bitrate may be estimated
from size/duration and is not a codec-quality guarantee.

An exact file URL selects that file only, so asking for a specific derivative
does not silently substitute its original. Stable media/archive identifiers
include the item identifier and logical filename. The initial requested URL is
preserved for credential scoping.

`media_types` filters logical assets before the `max_items` limit. Each result
reports `playlist_count`, `discovery_limited`, and counts of omitted private,
metadata, or malformed records. These counts describe this metadata response,
not an assertion that every file is accessible. Restricted/dark items and exact
private/missing files fail explicitly. Conflicting duplicate filenames fail
rather than choosing one silently. A malformed derivative cycle cannot hang
or discard its files.

## Verification

Offline fixtures cover URL validation, exact-file selection, AV grouping,
non-video assets, metadata filtering, private/restricted items, malformed records,
Unicode filenames, duplicate conflicts, cyclic derivatives, numeric metadata,
credential scoping, limits and stable identifiers.

Live checks on 2026-11-07 successfully read item metadata for `xfetch`,
`testmp3testfile`, and `ElephantsDream`. Two exact files were then extracted and
transferred through the native downloader:

| File | Bytes | SHA-256 |
| --- | ---: | --- |
| [Test MP3](https://archive.org/download/testmp3testfile/mpthreetest.mp3) | 198,658 | `07d11170fe30596ca17307682d2745e09862a8dcdf90c76fa90b70d381eb4973` |
| [XFETCH PDF](https://archive.org/download/xfetch/xfetch.pdf) | 419,170 | `fb581a20b92946d001699bdcd5ab2b214f5e577b3da10527d429e9cff384e65b` |

Both downloaded files also matched the MD5 and SHA-1 values in their source
metadata. Those live checks establish public audio/document extraction and transfer from
this host at the recorded time. They do not certify every Archive item, all
restricted access states, very large metadata responses, or full parity with
upstream's Internet Archive extractor.
