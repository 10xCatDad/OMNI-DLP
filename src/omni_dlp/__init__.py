"""Independent media-neutral downloading. Site compatibility is opt-in."""
from .models import DownloadResult, Media, OmniError

__version__ = "0.1.0a1"
__all__ = ["Media", "DownloadResult", "OmniError", "__version__"]
