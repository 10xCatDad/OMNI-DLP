"""Media-neutral records shared by discovery, transfer, and the public API."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class OmniError(Exception):
    """An actionable extraction or transfer failure."""


@dataclass
class Format:
    """One selectable representation of a logical media item.

    Codec ``none`` means the stream is absent; an empty codec is unknown.
    Bitrates are kilobits/second, sizes bytes, and duration seconds.
    """
    url: str
    format_id: str = ""
    ext: str = ""
    protocol: str = "http"
    vcodec: str = ""
    acodec: str = ""
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    tbr: float | None = None
    abr: float | None = None
    filesize: int | None = None
    language: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Media:
    url: str
    title: str = ""
    id: str = ""
    kind: str = "other"
    ext: str = ""
    mime_type: str = ""
    source_url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    formats: list[Format] = field(default_factory=list)
    requested_formats: list[Format] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DownloadResult:
    media: Media
    path: str
    size: int
    sha256: str
    skipped: bool = False
    attachments: list[DownloadResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
