"""Explicit, optional migration bridge to the independently installed yt-dlp.

Nothing in this module reimplements an upstream extractor or claims native
support. The CLI path intentionally leaves all argument parsing to yt-dlp.
"""
from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
from typing import Any

from .models import OmniError


_INSTALL_HINT = (
    "yt-dlp compatibility is not installed. Install the compatibility extra "
    "with: python -m pip install 'omni-dlp[compat]'"
)


def available() -> bool:
    """Whether this Python environment can locate the optional yt-dlp module.

    This checks installation, not the health of every extractor or dependency.
    """
    try:
        return importlib.util.find_spec("yt_dlp") is not None
    except (ImportError, ValueError):
        return False


def _require() -> None:
    if not available():
        raise OmniError(_INSTALL_HINT)


def run(arguments: list[str]) -> int:
    """Forward arguments unchanged and return yt-dlp's process return code.

    Standard input, standard output, standard error, environment, and working
    directory are inherited. No shell interprets the argument strings. Options,
    aliases, configuration files, authentication, and updates are upstream's
    responsibility in this explicit mode.
    """
    _require()
    return subprocess.run([sys.executable, "-m", "yt_dlp", *arguments], check=False).returncode


def download_urls(urls: list[str], options: dict[str, Any] | None = None) -> int:
    """Use the optional upstream Python API without translating its options.

    Callers choose this bridge explicitly. Return values and download errors
    follow yt-dlp's API; there is no conversion into native Media records.
    """
    _require()
    upstream = importlib.import_module("yt_dlp")
    with upstream.YoutubeDL(dict(options or {})) as downloader:
        return downloader.download(list(urls))
