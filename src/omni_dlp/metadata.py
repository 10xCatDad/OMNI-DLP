"""Embed selected metadata and local subtitles without replacing the source.

Only fixed descriptive fields are imported from extractor metadata. Existing
container metadata and all existing streams remain intact. FFmpeg/ffprobe see
local files only; neither subprocess may fetch URLs or follow playlist files.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from threading import Event

from .models import DownloadResult, OmniError
from .streams import _LOCAL_FORMATS, _check_cancelled, _execute, _ffmpeg, _publish, _temporary
from .transfer import _check_regular, _destination_lock, check_existing, file_digest, record_complete

_MUXERS = {"mp4": "mp4", "m4v": "mp4", "mov": "mov", "m4a": "ipod", "m4b": "ipod",
           "mkv": "matroska", "mka": "matroska", "mp3": "mp3", "flac": "flac",
           "ogg": "ogg", "oga": "ogg", "opus": "opus", "wav": "wav", "aif": "aiff", "aiff": "aiff"}
_CHAPTER_CONTAINERS = {"mp4", "m4v", "mov", "m4a", "m4b", "mkv", "mka", "mp3"}
_SUBTITLE_CONTAINERS = {"mp4", "m4v", "mov", "mkv", "mka"}
_MOV_CONTAINERS = {"mp4", "m4v", "mov", "m4a", "m4b"}
_SUBTITLE_FORMATS = {"srt": "srt", "vtt": "webvtt", "ass": "ass", "ssa": "ass"}
_INPUT_FORMATS = _LOCAL_FORMATS + ",srt,webvtt,ass"
_URL = re.compile(r"(?:https?|ftp|file)://[^\s<>]+", re.I)
_SECRET_LINE = re.compile(r"\b(?:authorization|cookie|set-cookie)\s*[:=][^\r\n]*", re.I)
_SECRET_VALUE = re.compile(r"\b(?:password|passwd|access_token|refresh_token|api[_-]?key|secret|token)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s;,]+)", re.I)

# ISO 639-1 to ISO 639-2/T identifiers. Only factual code pairs are included.
_LANG2 = dict(pair.split(":") for pair in (
    'aa:aar ab:abk af:afr ak:aka am:amh ar:ara an:arg as:asm av:ava ae:ave '
    'ay:aym az:aze ba:bak bm:bam be:bel bn:ben bi:bis bo:bod bs:bos br:bre '
    'bg:bul ca:cat cs:ces ch:cha ce:che cu:chu cv:chv kw:cor co:cos cr:cre '
    'cy:cym da:dan de:deu dv:div dz:dzo el:ell en:eng eo:epo et:est eu:eus '
    'ee:ewe fo:fao fa:fas fj:fij fi:fin fr:fra fy:fry ff:ful gd:gla ga:gle '
    'gl:glg gv:glv gn:grn gu:guj ht:hat ha:hau he:heb hz:her hi:hin ho:hmo '
    'hr:hrv hu:hun hy:hye ig:ibo io:ido ii:iii iu:iku ie:ile ia:ina id:ind '
    'ik:ipk is:isl it:ita jv:jav ja:jpn kl:kal kn:kan ks:kas ka:kat kr:kau '
    'kk:kaz km:khm ki:kik rw:kin ky:kir kv:kom kg:kon ko:kor kj:kua ku:kur '
    'lo:lao la:lat lv:lav li:lim ln:lin lt:lit lb:ltz lu:lub lg:lug mh:mah '
    'ml:mal mr:mar mk:mkd mg:mlg mt:mlt mn:mon mi:mri ms:msa my:mya na:nau '
    'nv:nav nr:nbl nd:nde ng:ndo ne:nep nl:nld nn:nno nb:nob no:nor ny:nya '
    'oc:oci oj:oji or:ori om:orm os:oss pa:pan pi:pli pl:pol pt:por ps:pus '
    'qu:que rm:roh ro:ron rn:run ru:rus sg:sag sa:san si:sin sk:slk sl:slv '
    'se:sme sm:smo sn:sna sd:snd so:som st:sot es:spa sq:sqi sc:srd sr:srp '
    'ss:ssw su:sun sw:swa sv:swe ty:tah ta:tam tt:tat te:tel tg:tgk tl:tgl '
    'th:tha ti:tir to:ton tn:tsn ts:tso tk:tuk tr:tur tw:twi ug:uig uk:ukr '
    'ur:urd uz:uzb ve:ven vi:vie vo:vol wa:wln wo:wol xh:xho yi:yid yo:yor '
    'za:zha zh:zho zu:zul iw:heb in:ind ji:yid '
).split())


def plan_output(path: str | Path) -> Path:
    """Plan a sibling output without creating or replacing anything."""
    source = Path(path)
    if not source.name or not source.suffix:
        raise OmniError("Metadata embedding requires a filename with a supported media extension")
    suffix = ".embedded" + source.suffix
    stem = source.stem.encode("utf-8")[:240 - len(suffix.encode("utf-8"))].decode("utf-8", errors="ignore")
    return source.with_name((stem.rstrip(" .") or "media") + suffix)


def _clean_text(value, field: str) -> str:
    if value is None:
        return ""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise OmniError(f"Metadata {field} must be text or a number")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    try:
        encoded_size = len(text.encode("utf-8"))
    except UnicodeError as exc:
        raise OmniError(f"Metadata {field} contains invalid Unicode") from exc
    if encoded_size > 1024 * 1024:
        raise OmniError(f"Metadata {field} exceeds the text size limit")
    text = "".join(character for character in text if character in "\n\t" or ord(character) >= 32 and ord(character) != 127)
    text = _URL.sub("[URL omitted]", text)
    text = _SECRET_LINE.sub("[credentials omitted]", text)
    return _SECRET_VALUE.sub("[credentials omitted]", text)


def _escape(value: str) -> str:
    # FFmetadata syntax uses backslash escapes, including physical newlines.
    for character in ("\\", "=", ";", "#"):
        value = value.replace(character, "\\" + character)
    return value.replace("\n", "\\\n")


def _tags(result: DownloadResult, extension: str) -> tuple[dict[str, str], list[str]]:
    metadata = result.media.metadata
    values = {"title": result.media.title or metadata.get("title"),
              "artist": metadata.get("artist") or metadata.get("uploader"),
              "album": metadata.get("album"), "description": metadata.get("description"),
              "date": metadata.get("date") or metadata.get("upload_date") or metadata.get("release_date"),
              "uploader": metadata.get("uploader")}
    tags = {key: _clean_text(value, key) for key, value in values.items() if value is not None and value != ""}
    omitted = []
    if extension in {"wav", "flac", "ogg", "oga", "opus"} and "description" in tags:
        # FFmpeg's RIFF/Vorbis comment mappings represent description as comment.
        tags["comment"] = tags.pop("description")
    if extension == "wav":
        # RIFF INFO has an artist field but no standard uploader tag.
        if "uploader" in tags:
            tags.pop("uploader")
            omitted.append("uploader")
    return tags, omitted


def _milliseconds(value, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)) or len(str(value)) > 64:
        raise OmniError(f"Chapter {field} must be a finite nonnegative time")
    try:
        seconds = Decimal(str(value))
        if not seconds.is_finite() or seconds < 0 or seconds > Decimal(2**63 - 1) / 1000:
            raise InvalidOperation
        milliseconds = int(seconds * 1000)
        if milliseconds > 2**63 - 1:
            raise InvalidOperation
        return milliseconds
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise OmniError(f"Chapter {field} must be a finite nonnegative time") from exc


def _chapters(result: DownloadResult) -> list[dict]:
    raw = result.media.metadata.get("chapters")
    if raw is None or raw == []:
        return []
    if not isinstance(raw, list) or not all(isinstance(chapter, dict) for chapter in raw):
        raise OmniError("Chapters must be a list of chapter objects")
    if len(raw) > 10000:
        raise OmniError("Chapter list exceeds the embedding limit")
    starts = [_milliseconds(chapter.get("start_time"), "start_time") for chapter in raw]
    if any(right <= left for left, right in zip(starts, starts[1:])):
        raise OmniError("Chapter start times must increase strictly")
    duration = result.media.metadata.get("duration")
    duration = _milliseconds(duration, "duration") if duration is not None else None
    chapters = []
    text_bytes = 0
    for index, (start, item) in enumerate(zip(starts, raw)):
        end = item.get("end_time")
        if end is None:
            end = starts[index + 1] if index + 1 < len(starts) else duration
            if end is None:
                raise OmniError("The final chapter requires end_time or media duration")
        else:
            end = _milliseconds(end, "end_time")
        if end <= start or index + 1 < len(starts) and end > starts[index + 1]:
            raise OmniError("Chapter intervals must have positive duration and must not overlap")
        if duration is not None and end > duration:
            raise OmniError("Chapter ends after the declared media duration")
        title = _clean_text(item.get("title") or f"Chapter {index + 1}", "chapter title")
        text_bytes += len(title.encode("utf-8"))
        if text_bytes > 8 * 1024 * 1024:
            raise OmniError("Chapter metadata exceeds the embedding size limit")
        chapters.append({"start": start, "end": end, "title": title})
    return chapters


def _language(value) -> str:
    if value is None or value == "":
        return "und"
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*", value):
        raise OmniError("Subtitle language must be an ISO language code, optionally with a region")
    base = value.split("-", 1)[0].lower()
    return _LANG2.get(base, base if len(base) == 3 else "und")


def _metadata_document(tags: dict[str, str], chapters: list[dict]) -> str:
    lines = [";FFMETADATA1"]
    # FFmpeg versions whose line reader checks only the immediately preceding
    # backslash merge the next line even after a doubled final backslash. Keep
    # those values out of the document; literal -metadata arguments set them.
    lines.extend(f"{key}={_escape(value)}" for key, value in tags.items() if not value.endswith("\\"))
    for chapter in chapters:
        lines.extend(["[CHAPTER]", "TIMEBASE=1/1000", f"START={chapter['start']}", f"END={chapter['end']}"])
        if not chapter["title"].endswith("\\"):
            lines.append("title=" + _escape(chapter["title"]))
    document = "\n".join(lines) + "\n"
    if len(document.encode("utf-8")) > 16 * 1024 * 1024:
        raise OmniError("Metadata document exceeds the embedding size limit")
    return document


def _probe(path: Path, cancel_event: Event | None = None) -> dict:
    executable = shutil.which("ffprobe")
    if not executable:
        raise OmniError("Metadata embedding requires ffprobe; install the FFmpeg tools")
    arguments = [executable, "-v", "error", "-protocol_whitelist", "file", "-format_whitelist", _INPUT_FORMATS,
                 "-show_streams", "-show_chapters", "-show_format", "-of", "json", str(path)]
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=output, stderr=errors)
        except OSError as exc:
            raise OmniError("Could not start ffprobe") from exc
        try:
            while True:
                _check_cancelled(cancel_event)
                if output.tell() > 8 * 1024 * 1024 or errors.tell() > 65536:
                    raise OmniError("Media probe exceeded its output limit")
                try:
                    code = process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        _check_cancelled(cancel_event)
        if code:
            raise OmniError("Cannot inspect local media for metadata embedding")
        if output.tell() > 8 * 1024 * 1024:
            raise OmniError("Media probe exceeded its output limit")
        output.seek(0)
        try:
            data = json.loads(output.read(8 * 1024 * 1024 + 1))
        except (ValueError, UnicodeError) as exc:
            raise OmniError("Media probe returned invalid metadata") from exc
        if not isinstance(data, dict) or not isinstance(data.get("streams"), list):
            raise OmniError("Media probe returned invalid stream information")
        return data


def _verified_output(source: dict, output: dict, tags: dict, chapters: list[dict], added: list[dict]) -> None:
    before = Counter((stream.get("codec_type"), stream.get("codec_name")) for stream in source["streams"])
    after = Counter((stream.get("codec_type"), stream.get("codec_name")) for stream in output["streams"])
    if any(after[key] < count for key, count in before.items()):
        raise OmniError("Embedding would drop or change an existing media stream")
    expected_subtitles = sum(stream.get("codec_type") == "subtitle" for stream in source["streams"]) + len(added)
    subtitle_streams = [stream for stream in output["streams"] if stream.get("codec_type") == "subtitle"]
    if len(subtitle_streams) != expected_subtitles:
        raise OmniError("Embedded subtitle count does not match the requested tracks")
    for track, stream in zip(added, subtitle_streams[-len(added):] if added else []):
        if stream.get("tags", {}).get("language", "und") != track["language"]:
            raise OmniError("Subtitle language was not preserved by the output container")
    actual_tags = {key.lower(): value for key, value in output.get("format", {}).get("tags", {}).items()}
    for stream in output["streams"]:
        for key, value in stream.get("tags", {}).items():
            actual_tags.setdefault(key.lower(), value)
    for key, value in tags.items():
        if actual_tags.get(key) != value:
            raise OmniError(f"The output container did not preserve the requested {key} tag")
    expected_chapters = chapters or source.get("chapters", [])
    actual_chapters = output.get("chapters", [])
    if len(actual_chapters) != len(expected_chapters):
        raise OmniError("The output container did not preserve the requested chapters")
    for expected, actual in zip(expected_chapters, actual_chapters):
        start = expected["start"] if chapters else float(expected.get("start_time", -1)) * 1000
        end = expected["end"] if chapters else float(expected.get("end_time", -1)) * 1000
        title = expected["title"] if chapters else expected.get("tags", {}).get("title")
        if (abs(float(actual.get("start_time", -1)) * 1000 - start) > 2
                or abs(float(actual.get("end_time", -1)) * 1000 - end) > 2
                or actual.get("tags", {}).get("title") != title):
            raise OmniError("The output container changed a requested chapter")


def embed_metadata(
    result: DownloadResult, *, subtitles: list[DownloadResult] | None = None,
    embed_tags: bool = True, embed_chapters: bool = True, embed_subtitles: bool = False,
    overwrite: bool = False, cancel_event: Event | None = None, max_filesize: int | None = None,
) -> DownloadResult:
    """Write a verified ``.embedded`` sibling, preserving the source and assets.

    Requested but absent chapters/subtitles are a no-op unless another embedding
    operation has actual data. Subtitles are appended; existing streams are
    copied. Newly supplied chapters replace existing chapters when present.
    """
    _check_cancelled(cancel_event)
    if max_filesize is not None and (isinstance(max_filesize, bool) or not isinstance(max_filesize, int) or max_filesize < 1):
        raise OmniError("Maximum embedded output size must be a positive integer")
    source = Path(result.path)
    extension = source.suffix.lstrip(".").lower()
    tags, omitted = _tags(result, extension) if embed_tags else ({}, [])
    chapters = _chapters(result) if embed_chapters else []
    selected = (subtitles or []) if embed_subtitles else []
    if not tags and not chapters and not selected:
        return result
    if extension not in _MUXERS:
        raise OmniError("Metadata embedding does not support this container; remux to MKV or MP4 first")
    if chapters and extension not in _CHAPTER_CONTAINERS:
        raise OmniError("This container does not support native chapter embedding; remux to MKV or MP4")
    if selected and extension not in _SUBTITLE_CONTAINERS:
        raise OmniError("This container does not support native subtitle embedding; remux to MKV or MP4")
    _check_regular(source)
    if not source.is_file():
        raise OmniError("Embedding input must be a regular downloaded file")
    source = source.resolve()
    destination = plan_output(source)
    _check_regular(destination)
    _check_regular(Path(str(destination) + ".omni.json"))
    tracks = []
    for item in selected:
        if not isinstance(item, DownloadResult):
            raise OmniError("Subtitle inputs must be completed download records")
        path = Path(item.path)
        _check_regular(path)
        if not path.is_file():
            raise OmniError("Embedding subtitle must be a regular downloaded file")
        subtitle_ext = (item.media.ext or path.suffix.lstrip(".")).lower()
        if subtitle_ext not in _SUBTITLE_FORMATS:
            raise OmniError("Native subtitle embedding accepts SRT, WebVTT, ASS, or SSA files")
        tracks.append({"path": path.resolve(), "format": _SUBTITLE_FORMATS[subtitle_ext],
                       "language": _language(item.media.metadata.get("language")),
                       "title": _clean_text(item.media.title, "subtitle title")})
    if any(track["path"] == destination for track in tracks):
        raise OmniError("Embedding output overlaps a subtitle input")
    _ffmpeg()
    with ExitStack() as stack:
        for path in sorted({source, destination, *(track["path"] for track in tracks)}, key=lambda p: str(p).casefold()):
            stack.enter_context(_destination_lock(path))
        _check_cancelled(cancel_event)
        source_digest = file_digest(source)
        profile = {"source": source_digest, "tags": tags, "chapters": chapters, "container": extension,
                   "flags": [embed_tags, embed_chapters, embed_subtitles],
                   "subtitles": [{"sha256": file_digest(track["path"]), "format": track["format"],
                                  "language": track["language"], "title": track["title"]} for track in tracks]}
        variant = "embed-v1:" + hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        media = replace(result.media, metadata={**result.media.metadata, "embedding": {
            "tags": sorted(tags), "chapters": len(chapters), "subtitles": len(tracks), "omitted_tags": omitted}})
        existing = check_existing(media, destination, overwrite, variant=variant)
        if existing is not None:
            return replace(existing, attachments=list(result.attachments))
        original = _probe(source, cancel_event)
        old_subtitles = sum(stream.get("codec_type") == "subtitle" for stream in original["streams"])
        metadata_text = _metadata_document(tags, chapters)
        descriptor, filename = tempfile.mkstemp(prefix=".omni-tags-", suffix=".ffmetadata", dir=destination.parent)
        document_path = Path(filename)
        temporary = None
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as document:
                document.write(metadata_text)
            temporary = _temporary(destination)
            # Keep source timestamps; automatic offset removal can shift MP3
            # chapter boundaries by the encoder-delay start time on a tag edit.
            arguments = ["-copyts", "-protocol_whitelist", "file", "-format_whitelist", _LOCAL_FORMATS, "-i", str(source)]
            for track in tracks:
                arguments += ["-protocol_whitelist", "file", "-format_whitelist", track["format"],
                              "-f", track["format"], "-i", str(track["path"])]
            metadata_index = 1 + len(tracks)
            arguments += ["-protocol_whitelist", "file", "-format_whitelist", "ffmetadata", "-f", "ffmetadata",
                          "-i", str(document_path), "-map", "0"]
            for index in range(len(tracks)):
                arguments += ["-map", f"{index + 1}:s:0"]
            # First metadata mapping wins on duplicate keys. New allowlisted
            # values take priority; unrelated original global tags remain.
            arguments += ["-map_metadata", str(metadata_index), "-map_metadata", "0",
                          "-map_chapters", str(metadata_index) if chapters else "0", "-c", "copy"]
            for key, value in tags.items():
                arguments += ["-metadata", key + "=" + value]
            for index, chapter in enumerate(chapters):
                arguments += [f"-metadata:c:{index}", "title=" + chapter["title"]]
            for index, track in enumerate(tracks, old_subtitles):
                codec = "mov_text" if extension in _MOV_CONTAINERS else "copy"
                arguments += [f"-c:s:{index}", codec, f"-metadata:s:s:{index}", "language=" + track["language"]]
                if track["title"]:
                    arguments += [f"-metadata:s:s:{index}", "title=" + track["title"]]
            if extension in {"ogg", "oga", "opus"}:
                for key, value in tags.items():
                    arguments += ["-metadata:s:a", key + "=" + value]
            if extension in _MOV_CONTAINERS:
                arguments += ["-movflags", "use_metadata_tags"]
            elif extension in {"aif", "aiff"}:
                arguments += ["-write_id3v2", "1"]
            arguments += ["-f", _MUXERS[extension]]
            _execute(arguments, temporary, max_filesize, cancel_event=cancel_event)
            _verified_output(original, _probe(temporary, cancel_event), tags, chapters, tracks)
            _check_cancelled(cancel_event)
            _publish(temporary, destination, overwrite)
            completed = record_complete(media, destination, variant=variant)
            return replace(completed, attachments=list(result.attachments))
        finally:
            document_path.unlink(missing_ok=True)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
