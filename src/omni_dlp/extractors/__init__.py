"""Explicit registry of independent built-in site extractors."""
from importlib import import_module

from .common import ExtractorContext
from ..models import Media

BUILTINS = (
    ("youtube", "YouTube individual videos; challenge-dependent coverage"),
    ("vimeo", "Vimeo public and unlisted player configuration"),
    ("bandcamp", "Bandcamp tracks and albums with embedded media metadata"),
    ("archive", "Internet Archive items and individual files"),
    ("wikimedia", "Wikimedia file pages and bounded category collections"),
    ("reddit", "Reddit public posts, galleries, and hosted media"),
    ("soundcloud", "SoundCloud public tracks, bounded sets, and artwork"),
)


def extract_builtin(url: str, **kwargs) -> list[Media] | None:
    for name, _ in BUILTINS:
        module = import_module(f"{__name__}.{name}")
        if module.supports(url):
            return module.extract(url, ExtractorContext(source_url=url, **kwargs))
    return None
