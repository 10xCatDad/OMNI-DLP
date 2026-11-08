"""Independent media-neutral downloading. Site compatibility is opt-in."""
from .models import DownloadResult, Format, Media, OmniError

__version__ = "0.3.0a1"
__all__ = ["Format", "Media", "DownloadResult", "OmniError", "__version__"]
