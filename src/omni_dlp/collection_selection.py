"""Native collection indices, dates and metadata filters, without code evaluation."""
from __future__ import annotations

import calendar
from dataclasses import dataclass, replace
from datetime import date as Date, datetime, timedelta, timezone
import math
import random
import re
from typing import Callable

from .models import Media, OmniError

_MAX_SPEC = 8192
_MAX_SEGMENTS = 1024
_MAX_INDEX = 10**12
_MAX_ENTRIES = 100000
_SCALAR_FIELDS = {"url", "title", "id", "kind", "ext", "mime_type", "source_url"}
_NUMERIC_FIELDS = {"duration", "view_count", "like_count", "dislike_count", "comment_count", "age_limit", "age",
                   "rating", "average_rating", "filesize", "filesize_approx", "width", "height", "fps",
                   "tbr", "abr", "timestamp", "release_timestamp", "playlist_index", "playlist_count"}


@dataclass
class CollectionOptions:
    playlist_items: str | None = None
    playlist_start: int = 1
    playlist_end: int | None = None
    playlist_reverse: bool = False
    playlist_random: bool = False
    random_seed: int | None = None
    date: str | None = None
    datebefore: str | None = None
    dateafter: str | None = None
    match_filter: str | None = None


@dataclass(frozen=True)
class _Span:
    start: int | None
    end: int | None
    step: int = 1
    single: bool = False
    infinite_end: bool = False

    @property
    def needs_count(self) -> bool:
        return ((self.start is None and self.step < 0)
                or (self.start is not None and self.start < 0)
                or (self.end is not None and self.end < 0))

    def bounds(self, count: int | None):
        if self.needs_count and count is None:
            raise OmniError("Negative playlist indices require a known source playlist_count; enumeration is incomplete")
        def resolve(value):
            return count + value + 1 if value is not None and value < 0 else value
        start = resolve(self.start)
        if start is None:
            start = 1 if self.step > 0 else count
        end = resolve(self.end)
        if self.single:
            end = start
        elif end is None:
            end = math.inf if self.step > 0 or self.infinite_end else 1
        return start, end

    def contains(self, index: int, count: int | None) -> bool:
        start, end = self.bounds(count)
        in_range = start <= index <= end if self.step > 0 else end <= index <= start
        return in_range and (index - start) % abs(self.step) == 0

    def expected_count(self, count: int) -> int:
        """How many actual source positions match, without iterating huge ranges."""
        start, end = self.bounds(count)
        if self.step > 0:
            low, high = max(1, start), min(count, end)
            first = start + max(0, (low - start + self.step - 1) // self.step) * self.step
            return max(0, (high - first) // self.step + 1) if first <= high else 0
        size = -self.step
        low, high = max(1, end), min(count, start)
        first = start - max(0, (start - high + size - 1) // size) * size
        return max(0, (first - low) // size + 1) if first >= low else 0


def _index(text: str | None) -> int | None:
    if text is None or text == "":
        return None
    if not re.fullmatch(r"[+-]?\d{1,13}", text):
        raise OmniError("Invalid playlist index; use integers within one trillion")
    value = int(text)
    if abs(value) > _MAX_INDEX:
        raise OmniError("Playlist index exceeds the supported bound")
    return value


def _spans(spec: str) -> list[_Span]:
    if not isinstance(spec, str) or not spec.strip() or len(spec) > _MAX_SPEC:
        raise OmniError("Playlist item expression must contain 1 to 8192 characters")
    segments = spec.split(",")
    if len(segments) > _MAX_SEGMENTS:
        raise OmniError("Too many playlist selection segments")
    result = []
    for segment in segments:
        segment = segment.strip()
        if re.fullmatch(r"[+-]?\d+", segment):
            result.append(_Span(_index(segment), None, single=True))
            continue
        if ":" in segment and re.fullmatch(r"[+-]?\d*", segment.split(":", 1)[0]):
            parts = segment.split(":")
            if len(parts) not in {2, 3}:
                raise OmniError("Playlist slices use START:STOP[:STEP]")
            start, end = parts[:2]
            step = parts[2] if len(parts) == 3 else None
        else:
            legacy = re.fullmatch(r"([+-]?\d+)?-([+-]?\d+|inf|infinite)?(?::([+-]?\d+))?", segment)
            if not legacy:
                raise OmniError("Invalid playlist selection; use indices, inclusive ranges, or slices")
            start, end, step = legacy.groups()
        if step == "":
            raise OmniError("Playlist slice step must be an integer")
        step_value = _index(step) if step is not None else 1
        if step_value == 0:
            raise OmniError("Playlist slice step cannot be zero")
        infinite = end in {"inf", "infinite"}
        result.append(_Span(_index(start), None if infinite else _index(end), step_value,
                            infinite_end=infinite))
    return result


def _prepare(entries: list[Media]) -> tuple[dict[int, Media], int | None, bool]:
    if not isinstance(entries, list) or len(entries) > _MAX_ENTRIES or not all(isinstance(e, Media) for e in entries):
        raise OmniError("Collection must contain at most 100000 Media records")
    indexed, counts, limited = {}, set(), False
    for position, entry in enumerate(entries, 1):
        if not isinstance(entry.metadata, dict):
            raise OmniError("Collection entry metadata must be a dictionary")
        metadata = entry.metadata
        index = metadata.get("playlist_index", position)
        if type(index) is not int or not 1 <= index <= _MAX_INDEX:
            raise OmniError("Source playlist_index must be a positive integer")
        if index in indexed:
            raise OmniError("Duplicate source playlist_index makes collection selection ambiguous")
        count = metadata.get("playlist_count")
        if count is not None:
            if type(count) is not int or not 0 <= count <= _MAX_INDEX:
                raise OmniError("Source playlist_count must be a nonnegative integer or unknown")
            counts.add(count)
        limited |= (metadata.get("collection_complete") is False or bool(metadata.get("collection_truncated"))
                    or bool(metadata.get("collection_skipped")) or bool(metadata.get("discovery_limited")))
        indexed[index] = replace(entry, metadata={**metadata, "playlist_index": index})
    if len(counts) > 1:
        raise OmniError("Conflicting source playlist_count values")
    count = next(iter(counts), None)
    if count is not None and any(index > count for index in indexed):
        raise OmniError("Source playlist_index exceeds playlist_count")
    contiguous = sorted(indexed) == list(range(1, len(indexed) + 1))
    complete = not limited and contiguous and (count is None or count == len(indexed))
    if count is None and complete:
        count = len(indexed)
    for entry in indexed.values():
        if count is not None:
            entry.metadata["playlist_count"] = count
        entry.metadata["collection_complete"] = complete
    return indexed, count, complete


def _utc_today() -> Date:
    return datetime.now(timezone.utc).date()


def _date(value: str, today: Date) -> Date:
    if not isinstance(value, str) or len(value) > 100:
        raise OmniError("Date must be YYYYMMDD, YYYY-MM-DD, or a supported relative date")
    try:
        if re.fullmatch(r"\d{8}", value):
            return Date(int(value[:4]), int(value[4:6]), int(value[6:]))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            return Date.fromisoformat(value)
        relative = re.fullmatch(r"(now|today|yesterday)(?:-(\d{1,6})(day|week|month|year)s?)?", value)
        if not relative:
            raise ValueError
        anchor, amount, unit = relative.groups()
        result = today - timedelta(days=1) if anchor == "yesterday" else today
        amount = int(amount or 0)
        if unit in {"month", "year"}:
            month_number = result.year * 12 + result.month - 1 - amount * (12 if unit == "year" else 1)
            year, month = divmod(month_number, 12)
            month += 1
            return Date(year, month, min(result.day, calendar.monthrange(year, month)[1]))
        return result - timedelta(days=amount * (7 if unit == "week" else 1))
    except (ValueError, OverflowError) as exc:
        raise OmniError("Invalid or unsupported collection date") from exc


def _date_range(options: CollectionOptions) -> tuple[Date, Date] | None:
    if options.date is not None and (options.datebefore is not None or options.dateafter is not None):
        raise OmniError("Choose --date or a --dateafter/--datebefore range")
    if options.date is None and options.datebefore is None and options.dateafter is None:
        return None
    today = _utc_today()
    lower = _date(options.date or options.dateafter, today) if options.date is not None or options.dateafter is not None else Date.min
    upper = _date(options.date or options.datebefore, today) if options.date is not None or options.datebefore is not None else Date.max
    if lower > upper:
        raise OmniError("Collection date range starts after it ends")
    return lower, upper


def _entry_date(entry: Media) -> Date | None:
    value = entry.metadata.get("upload_date")
    if value is not None and value != "":
        if not isinstance(value, str) or not re.fullmatch(r"\d{8}|\d{4}-\d{2}-\d{2}", value):
            raise OmniError("Source upload_date must use YYYYMMDD or YYYY-MM-DD")
        return _date(value, _utc_today())
    timestamp = entry.metadata.get("timestamp")
    if timestamp is None:
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        raise OmniError("Collection timestamp must be a finite number")
    try:
        if not math.isfinite(timestamp):
            raise OmniError("Collection timestamp must be a finite number")
        return datetime.fromtimestamp(timestamp, timezone.utc).date()
    except (ValueError, OverflowError, OSError) as exc:
        raise OmniError("Collection timestamp is outside the supported date range") from exc


def _conditions(expression: str) -> list[str]:
    if not isinstance(expression, str) or not expression.strip() or len(expression) > _MAX_SPEC:
        raise OmniError("Match filter must contain 1 to 8192 characters")
    parts, buffer, quote, escaped = [], [], "", False
    for char in expression:
        if escaped:
            buffer.append(char)
            escaped = False
        elif char == "\\":
            buffer.append(char)
            escaped = True
        elif quote:
            buffer.append(char)
            if char == quote:
                quote = ""
        elif char in {"'", '"'}:
            quote = char
            buffer.append(char)
        elif char == "&":
            parts.append("".join(buffer).strip())
            buffer.clear()
        else:
            buffer.append(char)
    if quote or escaped:
        raise OmniError("Unterminated quote or escape in match filter")
    parts.append("".join(buffer).strip())
    if len(parts) > 128 or any(not part for part in parts):
        raise OmniError("Match filter requires 1 to 128 nonempty conditions")
    return parts


def _literal(text: str) -> str:
    text = text.strip()
    if not text:
        raise OmniError("Match comparison requires a value")
    if text[0] in {"'", '"'}:
        if len(text) < 2 or text[-1] != text[0]:
            raise OmniError("Invalid quoted match-filter value")
        quote, text = text[0], text[1:-1]
    else:
        quote = ""
        if "|" in text:
            raise OmniError("Boolean OR is not supported in a match-filter expression; quote literal pipe characters")
        if any(char in text for char in "'\""):
            raise OmniError("Unexpected quote in match-filter value")
    output, escaped = [], False
    for char in text:
        if escaped:
            if char not in {"&", "\\", quote}:
                output.append("\\")
            output.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif quote and char == quote:
            raise OmniError("Unexpected quote in match-filter value")
        else:
            output.append(char)
    if escaped:
        raise OmniError("Unterminated match-filter escape")
    return "".join(output)


def _numeric(text: str) -> float | None:
    """Numeric values, decimal/binary file sizes, and explicit duration units."""
    match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+))\s*([KMGTPEZY]iB?|[kKmMgGtTpPeEzZyY][bB]?|[bB]?)", text)
    if match:
        try:
            value = int(match[1]) if re.fullmatch(r"[+-]?\d+", match[1]) else float(match[1])
        except (ValueError, OverflowError):
            return None
        suffix = match[2]
        if suffix and suffix not in {"b", "B"}:
            power = "KMGTPEZY".index(suffix[0].upper()) + 1
            # Preserve legacy lowercase binary units without treating kb as kB.
            binary = "i" in suffix or suffix[0].islower() and not suffix.endswith("b")
            value *= (1024 if binary else 1000) ** power
        if not isinstance(value, int) and not math.isfinite(value):
            return None
        return round(value) if suffix else value
    if re.fullmatch(r"\d+(?::\d{1,2}){1,2}(?:\.\d+)?", text):
        value = 0.0
        for part in text.split(":"):
            value = value * 60 + float(part)
        return value
    duration = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|second|seconds|min|minute|minutes|h|hr|hour|hours|d|day|days)", text, re.I)
    if duration:
        amount, unit = duration.groups()
        unit = unit.lower()
        factor = 86400 if unit.startswith("d") else 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
        value = float(amount) * factor
        return value if math.isfinite(value) else None
    return None


def _value(entry: Media, field: str):
    if field in _SCALAR_FIELDS:
        return getattr(entry, field)
    if field == "age" and field not in entry.metadata:
        return entry.metadata.get("age_limit")
    if field == "rating" and field not in entry.metadata:
        return entry.metadata.get("average_rating")
    return entry.metadata.get(field)


def _match_condition(condition: str) -> Callable[[Media], bool]:
    unary = re.fullmatch(r"(!)?\s*([a-z_][a-z_0-9]*)", condition)
    if unary:
        negative, field = unary.groups()
        def presence(entry):
            value = _value(entry, field)
            present = value if isinstance(value, bool) else value is not None
            return not present if negative else present
        return presence
    comparison = re.fullmatch(r"([a-z_][a-z_0-9]*)\s*(!\s*)?(<=|>=|\^=|\$=|\*=|=|<|>)(\s*\?)?\s*(.+)", condition)
    if not comparison:
        raise OmniError("Unsupported match-filter syntax; use scalar comparisons, field presence, and &")
    field, negative, operation, allow_unknown, expected_text = comparison.groups()
    expected = _literal(expected_text)
    numeric = _numeric(expected)
    if field in _NUMERIC_FIELDS and numeric is None:
        raise OmniError("Numeric match field requires a finite number, size, or duration")
    if field in _NUMERIC_FIELDS and operation in {"*=", "^=", "$="}:
        raise OmniError("String match operators cannot be used on numeric fields")

    def predicate(entry):
        actual = _value(entry, field)
        if isinstance(actual, float) and not math.isfinite(actual):
            actual = None
        if actual is None:
            return bool(allow_unknown)
        if not isinstance(actual, (str, int, float, bool)):
            raise OmniError("Match filters only compare scalar metadata values")
        target = expected
        if isinstance(actual, (int, float)):
            if numeric is None:
                raise OmniError("Numeric metadata cannot be compared with this match-filter value")
            if operation in {"*=", "^=", "$="}:
                raise OmniError("String match operators cannot be used on numeric metadata")
            target = numeric
        if operation == "=":
            accepted = actual == target
        elif operation == "<":
            accepted = actual < target
        elif operation == ">":
            accepted = actual > target
        elif operation == "<=":
            accepted = actual <= target
        elif operation == ">=":
            accepted = actual >= target
        elif operation == "*=":
            accepted = target in actual
        elif operation == "^=":
            accepted = actual.startswith(target)
        else:
            accepted = actual.endswith(target)
        return not accepted if negative else accepted
    return predicate


def select_entries(entries: list[Media], options: CollectionOptions | None = None) -> list[Media]:
    """Select copies of source entries, retaining original indices and counts.

    A valid selection with no matches returns []. Invalid syntax, conflicting
    metadata, or an end-relative request whose source entries were not enumerated
    raises OmniError. Random ordering uses a private generator and never changes
    the application's global random state.
    """
    options = options or CollectionOptions()
    if type(options.playlist_start) is not int or not 1 <= options.playlist_start <= _MAX_INDEX:
        raise OmniError("Playlist start must be a positive integer")
    if options.playlist_end is not None and (type(options.playlist_end) is not int or not -1 <= options.playlist_end <= _MAX_INDEX):
        raise OmniError("Playlist end must be a nonnegative integer, -1, or unspecified")
    if type(options.playlist_reverse) is not bool or type(options.playlist_random) is not bool:
        raise OmniError("Playlist ordering flags must be booleans")
    if options.random_seed is not None and type(options.random_seed) is not int:
        raise OmniError("Random seed must be an integer")
    end = "" if options.playlist_end in {None, -1} else str(options.playlist_end)
    spec = options.playlist_items if options.playlist_items is not None else f"{options.playlist_start}:{end}"
    spans = _spans(spec)
    date_range = _date_range(options)
    predicates = [_match_condition(part) for part in _conditions(options.match_filter)] if options.match_filter is not None else []
    indexed, count, complete = _prepare(entries)
    result, seen = [], set()
    indices = sorted(indexed)
    for span in spans:
        # Validate even an empty/incomplete collection before scanning candidates.
        span.bounds(count)
        selected = [index for index in (indices if span.step > 0 else reversed(indices)) if span.contains(index, count)]
        if span.needs_count and not complete and len(selected) != span.expected_count(count):
            raise OmniError("End-relative playlist selection references entries that were not enumerated; increase the discovery limit")
        for index in selected:
            if index not in seen:
                result.append(indexed[index])
                seen.add(index)
    if options.playlist_reverse:
        result.reverse()
    elif options.playlist_random:
        random.Random(options.random_seed).shuffle(result)
    filtered = []
    for entry in result:
        if date_range is not None:
            upload_date = _entry_date(entry)
            if upload_date is not None and not date_range[0] <= upload_date <= date_range[1]:
                continue
        if all(predicate(entry) for predicate in predicates):
            filtered.append(entry)
    return filtered
