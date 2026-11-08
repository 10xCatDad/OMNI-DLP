# Native HLS

Native HLS supports clear media and ordinary `METHOD=AES-128` segment encryption
with HTTP(S) key delivery. AES-128 here means full-segment AES-CBC with a binary
16-byte key, as specified by [RFC 8216](https://www.rfc-editor.org/rfc/rfc8216.html#section-4.3.2.4).
It does not imply support for DRM, SAMPLE-AES, FairPlay, or protected-license
acquisition.

```sh
omni-dlp 'https://example.org/stream.m3u8'
omni-dlp -F 'https://example.org/master.m3u8'
omni-dlp -f 'bv+ba/b' 'https://example.org/master.m3u8'
```

FFmpeg is required. Master discovery exposes video variants and external audio
renditions with quality, codec, language, and rendition-group metadata. A media
playlist exposes one format with unknown quality until its media is read.

## AES-128 behavior

- Identity keys are accepted when `KEYFORMAT` is absent or explicitly `identity`.
  The supported key-format version is 1, including version lists containing 1.
- Explicit initialization vectors must be hexadecimal unsigned integers no
  larger than 128 bits. An omitted IV uses the media sequence number.
- Rotation between publicly accessible HTTP(S) keys and IVs is supported by the
  FFmpeg HLS decoder. A later `METHOD=NONE` resets following segments to clear
  media.
- Initial key availability and the exact 16-byte length are checked before
  FFmpeg starts. Missing keys, invalid lengths, and unsupported encryption cause
  actionable errors. Wrong-key decoding failures do not publish completed media.
- Key bytes and key URLs are not saved in completion receipts. FFmpeg diagnostics
  containing signed URLs are not exposed as error text.
- FFmpeg's `crypto` protocol is enabled only after initial HLS manifests declare
  supported AES-128. Local conversion and multistream merge retain their separate
  local-file protocol restrictions.

## Network and recovery boundaries

The initial manifest traversal is bounded to 64 documents and 32 MiB total, with
at most 256 distinct keys. All encountered resource URLs must use HTTP(S).
Existing credentials embedded in URLs are rejected. Native adaptive downloading
continues to reject cookie files and sensitive/custom headers because FFmpeg
forwards headers differently from the native HTTP transfer client.

This validation is a bounded initial inspection, not an immutable manifest
snapshot or an authenticated forwarding proxy. FFmpeg fetches the manifests again
and can reload live playlists. A remote server can change content between those
requests. The FFmpeg protocol whitelist continues to prohibit `file`, `data`, and
other unsupported resource protocols; `tcp` and `tls` remain available as the
underlying transports for HTTP(S). Direct non-HTTP resource URLs in the inspected
manifests are rejected before starting FFmpeg. No stronger isolation against a
server changing those URLs after inspection is claimed.

Adaptive downloads restart after interruption; fragment-level resume and native
fragment retry/rate controls remain unfinished. Warnings from FFmpeg fail the
operation conservatively, including missing-segment warnings. The single exact
HLS diagnostic `mime type is not rfc8216 compliant` is accepted when FFmpeg exits
successfully: some origins serve valid playlists as `application/octet-stream`.
Any additional warning/error text, nonzero exit, or oversized diagnostic stream
still fails the operation. Completed files are
published atomically only after a successful operation. AES-CBC itself provides
no authenticity guarantee; successful decoding and output hashing do not prove
that a remote origin supplied an intended or trustworthy original.

## Verification

The tests generate and serve real encrypted HLS through a local HTTP server,
then check remuxed audio/video streams and decode the output with FFmpeg. Cases
cover explicit IVs, implicit sequence-number IVs, key rotation, missing keys,
short keys, wrong keys, master manifests, clear streams, unsupported SAMPLE-AES,
and prohibited `file`, `data`, nested `crypto`, and raw `tcp` key URLs.

The encrypted integration fixtures use MPEG-TS segments. Encrypted fragmented
MP4, byte-range segments, and I-frame trick-play have not been independently
validated.

The parser also tests mixed audio groups, quoted codec lists, relative URLs,
malformed attributes, oversized documents, unrecognized codecs, credential
scoping, and unsafe nonselected rendition URLs.
