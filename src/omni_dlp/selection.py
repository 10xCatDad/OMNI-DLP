"""Independent, bounded format selection and ranking.

Each result group is one output. More than one format in a group requests a
merge; separate groups request separate outputs. No upstream code is imported.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Callable

from .models import Format, OmniError
from .types import EXTENSION_KINDS

_MAX_EXPRESSION = 8192
_MAX_DEPTH = 48
_MAX_GROUPS = 4096
_MAX_FORMATS = 10000
_AUDIO_EXTS = {"aac", "aiff", "alac", "flac", "m4a", "m4b", "mp3", "oga", "ogg", "opus", "wav"}
_VIDEO_EXTS = {"3gp", "avi", "flv", "mkv", "mov", "mp4", "m4v", "ogv", "ts", "webm"}
_OTHER_EXTS = {ext for ext, kind in EXTENSION_KINDS.items() if kind not in {"video", "audio"}} | {"mhtml", "json", "xml"}
_VIDEO_ORDER = ("theora", "h263", "vp8", "h264", "h265", "vp9", "vp9.2", "av01")
_AUDIO_ORDER = ("dts", "ac3", "eac3", "ac4", "mp3", "mp4a", "aac", "vorbis", "opus", "wav", "aiff", "alac", "flac")
_VIDEO_EXT_ORDER = ("flv", "webm", "mov", "mp4")
_AUDIO_EXT_ORDER = ("webm", "opus", "ogg", "mp3", "aac", "m4a")
_PROTOCOL_ORDER = ("f4m", "f4f", "websocket_frag", "dash", "http_dash_segments", "hls", "m3u8", "m3u8_native", "ftp", "http", "ftps", "https")
_HDR_ORDER = {"SDR": 1, "HLG": 2, "HDR10": 3, "HDR10+": 4, "HDR12": 5, "DV": 6}
_ALIASES = {"protocol": "proto", "language": "lang", "format_id": "id", "filesize_approx": "fs_approx",
            "audio_channels": "channels", "preference": "ie_pref", "source_preference": "source",
            "language_preference": "lang", "dynamic_range": "hdr"}
_SORT_FIELDS = {"hasvid", "hasaud", "ie_pref", "lang", "quality", "source", "proto", "vcodec", "acodec",
                "vext", "aext", "filesize", "fs_approx", "size", "height", "width", "res", "fps", "hdr",
                "channels", "tbr", "vbr", "abr", "br", "asr", "id"}
_NUMERIC_FIELDS = {"width", "height", "fps", "tbr", "abr", "vbr", "asr", "filesize", "filesize_approx",
                   "duration", "audio_channels", "channels", "preference", "quality", "source_preference",
                   "language_preference"}
_STRING_FIELDS = {"format_id", "ext", "vcodec", "acodec", "protocol", "language", "format", "format_note",
                  "resolution", "dynamic_range", "container", "video_ext", "audio_ext"}
_DEFAULT_SORT = "lang,quality,res,fps,hdr:12,vcodec,channels,acodec,size,br,asr,proto,ext,hasaud,source,id"


def _number(value) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _quantity(value: str) -> float:
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([kKmMgGtTpPeE]?(?:i?[bB])?)", value)
    if not match:
        raise OmniError("Invalid numeric format value")
    number = float(match[1])
    suffix = match[2].upper().removesuffix("B").removesuffix("I")
    if suffix:
        number *= 1024 ** ("KMGTPE".index(suffix) + 1)
    if not math.isfinite(number):
        raise OmniError("Format values must be finite")
    return number


def _field(fmt: Format, name: str):
    if name == "video_ext":
        return "none" if fmt.vcodec == "none" else fmt.ext
    if name == "audio_ext":
        return "none" if fmt.acodec == "none" else fmt.ext
    if name == "resolution":
        return fmt.metadata.get(name) or (f"{fmt.width}x{fmt.height}" if fmt.width and fmt.height else None)
    if name == "channels":
        name = "audio_channels"
    return getattr(fmt, name, fmt.metadata.get(name))


def _present(codec: str) -> bool:
    # Like upstream's format records, only explicit "none" proves absence.
    return codec != "none"


def _playable(fmt: Format) -> bool:
    return _present(fmt.vcodec) or _present(fmt.acodec)


def _codec(value: str, audio: bool = False) -> str:
    value = str(value).lower()
    families = (("avc", "h264"), ("h.264", "h264"), ("hev", "h265"), ("hvc", "h265"),
                ("hevc", "h265"), ("h.265", "h265"), ("av1", "av01"), ("vp09.02", "vp9.2"),
                ("vp09", "vp9"), ("vp9.2", "vp9.2"), ("vp9", "vp9"),
                ("mp4a", "mp4a"), ("pcm_s16be", "aiff"), ("pcm_", "wav"))
    for prefix, replacement in families:
        if value.startswith(prefix):
            return replacement
    order = _AUDIO_ORDER if audio else _VIDEO_ORDER
    for family in reversed(order):
        if value.startswith(family):
            return family
    return value


def _rank(value, order: tuple[str, ...]) -> float | None:
    if value is None or value == "":
        return None
    if value == "none":
        return -1
    return order.index(value) + 1 if value in order else 0


def _sort_value(fmt: Format, name: str):
    if name == "hasvid":
        return int(_present(fmt.vcodec))
    if name == "hasaud":
        return int(_present(fmt.acodec))
    if name == "vcodec":
        return _rank(_codec(fmt.vcodec), _VIDEO_ORDER)
    if name == "acodec":
        return _rank(_codec(fmt.acodec, True), _AUDIO_ORDER)
    if name == "vext":
        return _rank(fmt.ext, _VIDEO_EXT_ORDER)
    if name == "aext":
        return _rank(fmt.ext, _AUDIO_EXT_ORDER)
    if name == "proto":
        return _rank(fmt.protocol, _PROTOCOL_ORDER)
    if name == "id":
        # Numeric IDs should compare numerically, with strings in a separate bucket.
        return (1, number) if (number := _number(fmt.format_id)) is not None else (0, fmt.format_id)
    if name == "res":
        dimensions = [value for value in (_number(fmt.width), _number(fmt.height)) if value is not None]
        return min(dimensions) if dimensions else None
    if name == "size":
        return _number(fmt.filesize) if _number(fmt.filesize) is not None else _number(fmt.metadata.get("filesize_approx"))
    if name == "br":
        total = _number(fmt.tbr)
        if total is not None:
            return total
        components = [_number(fmt.metadata.get("vbr")), _number(fmt.abr)]
        return sum(x for x in components if x is not None) if any(x is not None for x in components) else None
    metadata = {"ie_pref": "preference", "lang": "language_preference", "source": "source_preference",
                "fs_approx": "filesize_approx", "channels": "audio_channels"}
    if name == "hdr":
        return _HDR_ORDER.get(str(fmt.metadata.get("dynamic_range", "")).upper())
    return _number(_field(fmt, metadata.get(name, name)))


def _target(field: str, text: str):
    if field in {"vcodec", "acodec"}:
        order = _AUDIO_ORDER if field == "acodec" else _VIDEO_ORDER
        value = _codec(text, field == "acodec")
        if value not in {*order, "none"}:
            raise OmniError("Unknown codec sorting target")
        return _rank(value, order)
    if field in {"vext", "aext"}:
        order = _VIDEO_EXT_ORDER if field == "vext" else _AUDIO_EXT_ORDER
        if text not in {*order, "none"}:
            raise OmniError("Unknown extension sorting target")
        return _rank(text, order)
    if field == "proto":
        if text not in _PROTOCOL_ORDER:
            raise OmniError("Unknown protocol sorting target")
        return _rank(text, _PROTOCOL_ORDER)
    if field == "hdr":
        if text in {"12", "HDR12"}:
            return _HDR_ORDER["HDR12"]
        if text.upper() in _HDR_ORDER:
            return _HDR_ORDER[text.upper()]
        raise OmniError("Unknown dynamic-range sorting target")
    if field == "id":
        raise OmniError("Format ID sorting does not support targets")
    return _quantity(text)


@dataclass(frozen=True)
class _Sort:
    field: str
    ascending: bool = False
    target: float | None = None
    nearest: bool = False

    def key(self, fmt: Format):
        value = _sort_value(fmt, self.field)
        if value is None:
            return (0, 0, 0)
        if self.field == "id":
            # Reverse lexicographic strings without relying on an arbitrary alphabet.
            kind, value = value
            if self.ascending:
                value = -value if kind else tuple(-ord(char) for char in value) + (1,)
            return (1, kind, value)
        if self.target is not None:
            if self.nearest:
                return (1, -abs(value - self.target), -value if not self.ascending else value)
            preferred = value >= self.target if self.ascending else value <= self.target
            return (1, int(preferred), (-value if self.ascending else value) if preferred else (value if self.ascending else -value))
        return (1, -value if self.ascending else value, 0)


def _sort_spec(sort: str | None) -> list[_Sort]:
    if sort is not None and not isinstance(sort, str):
        raise OmniError("Format sort must be a string")
    if sort and len(sort) > _MAX_EXPRESSION:
        raise OmniError("Format sort expression is too long")
    parts = sort.split(",") if sort else []
    if len(parts) > 64 or any(not part.strip() for part in parts):
        raise OmniError("Invalid format sort expression")
    result = [_Sort("hasvid"), _Sort("ie_pref")]
    seen = {"hasvid", "ie_pref"}
    for part in parts + _DEFAULT_SORT.split(","):
        match = re.fullmatch(r"\s*(\+?)([A-Za-z_]+)(?:([:~])([^\s,]+))?\s*", part)
        if not match:
            raise OmniError("Invalid or unsupported format sort expression")
        ascending, name, delimiter, target = match.groups()
        name = _ALIASES.get(name, name)
        if name in {"ext", "codec", "br"}:
            fields = {"ext": ("vext", "aext"), "codec": ("vcodec", "acodec"), "br": ("br",)}[name]
            targets = target.split(":") if target is not None and delimiter == ":" else [target]
            if len(targets) > len(fields):
                raise OmniError("Too many preferred values in format sort")
            targets += [None] * (len(fields) - len(targets))
        else:
            fields, targets = (name,), [target]
        for field, value in zip(fields, targets):
            if field not in _SORT_FIELDS:
                raise OmniError(f"Unsupported format sort field: {field}")
            parsed = _target(field, value) if value is not None else None
            if field in seen:
                continue
            result.append(_Sort(field, bool(ascending), parsed, delimiter == "~"))
            seen.add(field)
    return result


def sort_formats(formats: list[Format], sort: str | None = None) -> list[Format]:
    """Return a new list ordered from worst to best using native ranking."""
    if len(formats) > _MAX_FORMATS or not all(isinstance(fmt, Format) for fmt in formats):
        raise OmniError("Format list must contain at most 10000 Format records")
    spec = _sort_spec(sort)
    return sorted(formats, key=lambda fmt: tuple(field.key(fmt) for field in spec))


def _unquote(value: str) -> str:
    value = value.strip()
    if not value:
        raise OmniError("Format filter requires a comparison value")
    if value[0] not in {"'", '"'}:
        if any(c.isspace() for c in value) or value[-1] in {"'", '"'}:
            raise OmniError("Quote format filter values containing spaces")
        return value
    quote = value[0]
    if len(value) < 2 or value[-1] != quote:
        raise OmniError("Unterminated format filter value")
    result, escaped = [], False
    for char in value[1:-1]:
        if escaped:
            if char not in {quote, "\\"}:
                result.append("\\")
            result.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            raise OmniError("Unexpected quote in format filter")
        else:
            result.append(char)
    if escaped:
        raise OmniError("Unterminated format filter escape")
    return "".join(result)


def _predicate(expression: str) -> Callable[[Format], bool]:
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z_0-9]*)\s*(!?\^=|!?\$=|!?\*=|!=|<=|>=|=|<|>)(\?)?\s*(.*?)\s*", expression)
    if not match:
        raise OmniError("Invalid or unsupported format filter")
    field, operation, allow_unknown, text = match.groups()
    if field not in _NUMERIC_FIELDS | _STRING_FIELDS:
        raise OmniError(f"Unsupported format filter field: {field}")
    expected = _unquote(text)
    if field in _NUMERIC_FIELDS:
        if operation not in {"=", "!=", "<", ">", "<=", ">="}:
            raise OmniError("Numeric format fields require a numeric comparison")
        expected = _quantity(expected)
    elif operation in {"<", ">", "<=", ">="}:
        raise OmniError("String format fields support equality, prefix, suffix, and contains comparisons")

    def matches(fmt: Format) -> bool:
        actual = _field(fmt, field)
        if field in _NUMERIC_FIELDS:
            actual = _number(actual)
        if actual is None or actual == "":
            return bool(allow_unknown)
        if field in _STRING_FIELDS:
            actual = str(actual)
        if operation == "=":
            return actual == expected
        if operation == "!=":
            return actual != expected
        if operation == "<":
            return actual < expected
        if operation == ">":
            return actual > expected
        if operation == "<=":
            return actual <= expected
        if operation == ">=":
            return actual >= expected
        negative = operation.startswith("!")
        operator = operation.removeprefix("!")
        result = actual.startswith(expected) if operator == "^=" else actual.endswith(expected) if operator == "$=" else expected in actual
        return not result if negative else result
    return matches


@dataclass
class _Node:
    kind: str
    value: str = ""
    children: tuple[_Node, ...] = ()
    filters: tuple[Callable[[Format], bool], ...] = ()


class _Parser:
    def __init__(self, expression: str):
        if not isinstance(expression, str) or not expression.strip() or len(expression) > _MAX_EXPRESSION:
            raise OmniError("Format expression must be a nonempty string of at most 8192 characters")
        self.text, self.pos, self.depth, self.nodes = expression, 0, 0, 0

    def token(self) -> str:
        while self.pos < len(self.text) and self.text[self.pos].isspace():
            self.pos += 1
        return self.text[self.pos:self.pos + 1]

    def node(self, kind, value="", children=()):
        self.nodes += 1
        if self.nodes > 512:
            raise OmniError("Format expression has too many selectors")
        return _Node(kind, value, tuple(children))

    def parse(self) -> _Node:
        result = self.sequence(",", lambda: self.sequence("/", lambda: self.sequence("+", self.primary)))
        if self.token():
            raise OmniError("Unexpected token in format expression")
        return result

    def sequence(self, delimiter, child):
        parts = [child()]
        while self.token() == delimiter:
            self.pos += 1
            parts.append(child())
        return parts[0] if len(parts) == 1 else self.node(delimiter, children=parts)

    def primary(self) -> _Node:
        token = self.token()
        if token == "(":
            self.pos += 1
            self.depth += 1
            if self.depth > _MAX_DEPTH:
                raise OmniError("Format expression is nested too deeply")
            inside = self.sequence(",", lambda: self.sequence("/", lambda: self.sequence("+", self.primary)))
            if self.token() != ")":
                raise OmniError("Unclosed format selector group")
            self.pos += 1
            self.depth -= 1
            result = self.node("group", children=(inside,))
        elif token == "[":
            result = self.node("atom", "best")
        else:
            start = self.pos
            while self.pos < len(self.text) and self.text[self.pos] not in ",/+()[]" and not self.text[self.pos].isspace():
                self.pos += 1
            value = self.text[start:self.pos]
            if not re.fullmatch(r"[A-Za-z0-9_.:-]+\*?(?:\.[1-9][0-9]*)?", value):
                raise OmniError("Invalid or unsupported format selector")
            if value == "mergeall":
                raise OmniError("mergeall requires a multistream policy and is not supported by this selector")
            result = self.node("atom", value)
        filters = []
        while self.token() == "[":
            self.pos += 1
            start, quote, escaped = self.pos, "", False
            while self.pos < len(self.text):
                char = self.text[self.pos]
                if escaped:
                    escaped = False
                elif quote and char == "\\":
                    escaped = True
                elif quote and char == quote:
                    quote = ""
                elif not quote and char in {"'", '"'}:
                    quote = char
                elif not quote and char == "]":
                    break
                self.pos += 1
            if self.pos == len(self.text):
                raise OmniError("Unclosed format filter")
            filters.append(_predicate(self.text[start:self.pos]))
            self.pos += 1
        result.filters = tuple(filters)
        return result


def _atom(name: str, formats: list[Format]) -> list[list[Format]]:
    if name == "all":
        return [[fmt] for fmt in reversed(formats)]
    match = re.fullmatch(r"(best|worst|b|w)(video|audio|v|a)?(\*)?(?:\.([1-9]\d*))?", name)
    if match:
        direction, kind, star, nth = match.groups()
        kind = kind[0] if kind else ""
        def predicate(fmt):
            if kind == "v":
                return _present(fmt.vcodec) if star else fmt.acodec == "none"
            if kind == "a":
                return _present(fmt.acodec) if star else fmt.vcodec == "none"
            return bool(star) or (_present(fmt.vcodec) and _present(fmt.acodec))
        matches = [fmt for fmt in formats if _playable(fmt) and predicate(fmt)]
        if not kind and formats and all(not _playable(fmt) for fmt in formats):
            # Images and other assets are first-class formats in the native API.
            matches = formats.copy()
        if not matches and not kind and not star and (
            all(fmt.vcodec == "none" for fmt in formats) or all(fmt.acodec == "none" for fmt in formats)
        ):
            matches = [fmt for fmt in formats if _playable(fmt)]
        if direction.startswith("b"):
            matches.reverse()
        index = int(nth or 1) - 1
        return [[matches[index]]] if index < len(matches) else []
    if name in _AUDIO_EXTS:
        matches = [fmt for fmt in formats if fmt.ext == name and _present(fmt.acodec)]
    elif name in _VIDEO_EXTS:
        matches = [fmt for fmt in formats if fmt.ext == name and _present(fmt.vcodec) and _present(fmt.acodec)]
        if not matches and not any(_present(fmt.vcodec) and _present(fmt.acodec) for fmt in formats):
            matches = [fmt for fmt in formats if fmt.ext == name and _present(fmt.vcodec)]
    elif name in _OTHER_EXTS:
        matches = [fmt for fmt in formats if fmt.ext == name]
    else:
        matches = [fmt for fmt in formats if fmt.format_id == name]
    return [[matches[-1]]] if matches else []


def _merge(groups: tuple[list[Format], ...]) -> list[Format]:
    result, video, audio = [], False, False
    for group in groups:
        for fmt in group:
            has_video, has_audio = _present(fmt.vcodec), _present(fmt.acodec)
            if not _playable(fmt) or (has_video and video) or (has_audio and audio):
                continue
            result.append(fmt)
            video |= has_video
            audio |= has_audio
    return result


def _compatible_pool(formats: list[Format], chosen: list[Format]) -> list[Format]:
    groups: dict[str, set[str]] = {}
    for fmt in chosen:
        group = fmt.metadata.get("audio_group")
        manifest = fmt.metadata.get("manifest_url", "")
        if isinstance(group, str) and group and isinstance(manifest, str):
            groups.setdefault(manifest, set()).add(group)
    def compatible(fmt):
        group = fmt.metadata.get("audio_group")
        manifest = fmt.metadata.get("manifest_url", "")
        if not isinstance(group, str) or not isinstance(manifest, str) or manifest not in groups:
            return True
        return not group or group in groups[manifest]
    return [fmt for fmt in formats if compatible(fmt)]


def _evaluate(node: _Node, formats: list[Format]) -> list[list[Format]]:
    if node.filters:
        formats = [fmt for fmt in formats if all(predicate(fmt) for predicate in node.filters)]
    if node.kind == "atom":
        return _atom(node.value, formats)
    if node.kind == "group":
        return _evaluate(node.children[0], formats)
    if node.kind == "+":
        result = _evaluate(node.children[0], formats)
        for child in node.children[1:]:
            combined = []
            for left in result:
                selected = _evaluate(child, _compatible_pool(formats, left))
                if len(combined) + len(selected) > _MAX_GROUPS:
                    raise OmniError("Format expression would produce too many outputs")
                combined.extend(_merge((left, right)) for right in selected)
            result = [group for group in combined if group]
            if not result:
                break
        return result
    result = []
    for child in node.children:
        selected = _evaluate(child, formats)
        if node.kind == "/":
            if selected:
                return selected
        elif node.kind == ",":
            result.extend(selected)
        if len(result) > _MAX_GROUPS:
            raise OmniError("Format expression would produce too many outputs")
    return result


def select_formats(
    formats: list[Format], expression: str = "bestvideo+bestaudio/best", sort: str | None = None,
) -> list[list[Format]]:
    """Select output groups; return [] for valid expressions with no matches.

    At most one audio-bearing and one video-bearing representation is retained
    per merge, matching the default upstream single-stream policy. Unknown syntax
    raises OmniError and never silently selects an unrelated default format.
    """
    tree = _Parser(expression).parse()
    ordered = sort_formats(formats, sort)
    result = _evaluate(tree, ordered)
    if len(result) > _MAX_GROUPS:
        raise OmniError("Format expression would produce too many outputs")
    return result
