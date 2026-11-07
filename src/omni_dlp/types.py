"""Small, extensible registry for media classification, not a file allowlist."""
from __future__ import annotations

import mimetypes
from urllib.parse import unquote, urlsplit

_EXTENSIONS = {
    "image": "jpg jpeg jpe png apng gif webp avif heic heif tif tiff bmp svg ico jxl psd raw dng cr2 nef exr hdr",
    "audio": "mp3 m4a m4b aac wav flac ogg oga opus aiff aif alac wma amr mid midi ape ac3 dts mka au",
    "video": "mp4 m4v mov mkv webm avi flv wmv mpg mpeg m2v ts mts m2ts ogv 3gp 3g2 vob mxf m3u8 mpd",
    "document": "pdf epub mobi azw azw3 cbz cbr txt md rtf doc docx odt ppt pptx odp xls xlsx ods csv",
    "archive": "zip tar gz bz2 xz zst 7z rar tgz tbz2 iso dmg cab",
    "font": "woff woff2 ttf otf eot",
    "model": "gltf glb obj stl fbx blend usd usdz usdc usda ply 3mf",
    "subtitle": "srt vtt ass ssa ttml dfxp sub sbv lrc",
}
EXTENSION_KINDS = {ext: kind for kind, values in _EXTENSIONS.items() for ext in values.split()}
MEDIA_KINDS = frozenset((*_EXTENSIONS, "other"))
_MIME_EXT = {
    "image/jpeg": "jpg", "image/png": "png", "image/svg+xml": "svg",
    "image/avif": "avif", "image/webp": "webp", "image/heic": "heic",
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/ogg": "ogg",
    "audio/x-wav": "wav", "audio/wav": "wav", "audio/flac": "flac",
    "video/mp4": "mp4", "video/webm": "webm", "video/mp2t": "ts",
    "application/pdf": "pdf", "application/epub+zip": "epub",
    "application/zip": "zip", "application/gzip": "gz",
    "application/vnd.apple.mpegurl": "m3u8", "application/x-mpegurl": "m3u8",
    "audio/mpegurl": "m3u8", "audio/x-mpegurl": "m3u8",
    "application/dash+xml": "mpd", "text/vtt": "vtt",
    "application/x-subrip": "srt", "model/gltf+json": "gltf",
    "model/gltf-binary": "glb", "font/woff": "woff", "font/woff2": "woff2",
}
MANIFEST_MIMES = frozenset(k for k, v in _MIME_EXT.items() if v in {"m3u8", "mpd"})
DOCUMENT_MIMES = frozenset({"text/html", "application/xhtml+xml", "application/json", "application/feed+json", "application/rss+xml", "application/atom+xml", "text/xml", "application/xml"})


def extension(url: str) -> str:
    name = unquote(urlsplit(url).path).rsplit("/", 1)[-1]
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    return suffix if suffix.isascii() and suffix.isalnum() and len(suffix) <= 12 else ""


def classify(url: str, mime_type: str = "", filename: str = "") -> tuple[str, str]:
    """Return kind and extension. Unknown formats remain downloadable as other."""
    mime = mime_type.split(";", 1)[0].strip().lower()
    ext = extension(filename or url)
    if mime in MANIFEST_MIMES:
        return "video", _MIME_EXT[mime]
    # Authoritative MIME corrects misleading endpoint suffixes, e.g. image.php.
    if mime and mime not in {"application/octet-stream", "binary/octet-stream"}:
        guessed = _MIME_EXT.get(mime) or (mimetypes.guess_extension(mime) or "").lstrip(".")
        major = mime.split("/", 1)[0]
        if major in {"image", "audio", "video", "font", "model"}:
            if EXTENSION_KINDS.get(ext) != major:
                ext = guessed or ext
            return major, ext
        if guessed and (not ext or ext not in EXTENSION_KINDS):
            ext = guessed
    kind = EXTENSION_KINDS.get(ext, "other")
    if kind == "other" and mime.startswith("text/") and mime not in DOCUMENT_MIMES:
        kind = "document"
    return kind, ext
