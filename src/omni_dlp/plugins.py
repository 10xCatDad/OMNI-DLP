"""Installed Python packages may register the omni_dlp.extractors entry point.

An entry point loads an object exposing supports(url) and discover(url, **kwargs).
Installing a plugin grants it normal Python code execution privileges.
"""
from importlib.metadata import entry_points

from .models import Media, OmniError


def discover_plugin(url: str, **kwargs) -> list[Media] | None:
    for entry in sorted(entry_points(group="omni_dlp.extractors"), key=lambda ep: ep.name):
        try:
            plugin = entry.load()
            if plugin.supports(url):
                result = list(plugin.discover(url, **kwargs))
                if not all(isinstance(item, Media) for item in result):
                    raise TypeError("extractor must return Media objects")
                return result
        except Exception as exc:
            raise OmniError(f"Extractor plugin {entry.name!r} failed ({type(exc).__name__})") from exc
    return None
