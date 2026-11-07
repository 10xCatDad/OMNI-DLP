"""Media-neutral records shared by discovery, transfer, and the public API."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


class OmniError(Exception):
    """An actionable extraction or transfer failure."""


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DownloadResult:
    media: Media
    path: str
    size: int
    sha256: str
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
