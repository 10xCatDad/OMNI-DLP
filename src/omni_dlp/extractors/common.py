"""Small bounded HTTP client for independently implemented site extractors."""
from __future__ import annotations

from dataclasses import dataclass, field
import http.client
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request

from ..models import OmniError
from ..transfer import _origin, build_opener, request_headers, request_url


@dataclass
class ExtractorContext:
    source_url: str
    headers: dict[str, str] = field(default_factory=dict)
    cookies: str | None = None
    timeout: float = 30
    max_items: int = 1000
    media_types: set[str] | None = None
    max_document: int = 8 * 1024 * 1024
    metadata_only: bool = False

    def scoped_headers(self, url: str) -> dict[str, str]:
        headers = request_headers(self.headers)
        if _origin(url) != _origin(self.source_url):
            headers = {k: v for k, v in headers.items()
                       if k.lower() in {"user-agent", "accept", "accept-language", "accept-encoding"}}
        return headers

    def get_document(self, url: str, *, headers: dict[str, str] | None = None) -> tuple[bytes, str, dict[str, str]]:
        """Fetch bytes with the final URL and headers safe for relative children.

        Context headers belong to source_url; explicit headers belong to this
        target URL. The redirect handler strips credentials when origins change.
        """
        try:
            outgoing = request_headers({**self.scoped_headers(url), **(headers or {})})
            request = Request(request_url(url), headers=outgoing)
            with build_opener(self.cookies).open(request, timeout=self.timeout) as response:
                if response.headers.get("Content-Encoding", "identity").lower() not in {"", "identity"}:
                    raise OmniError("Site ignored the requested identity content encoding")
                data = response.read(self.max_document + 1)
                if len(data) > self.max_document:
                    raise OmniError("Site response exceeds the extractor document limit")
                final_url = response.geturl()
                if _origin(final_url) != _origin(url):
                    outgoing = {k: v for k, v in outgoing.items() if k.lower() in {
                        "user-agent", "accept", "accept-language", "accept-encoding"}}
                return data, final_url, outgoing
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise OmniError(f"Site request failed with HTTP {code}") from exc
        except (URLError, OSError, http.client.HTTPException) as exc:
            raise OmniError("Site request failed; check network access and authentication") from exc

    def get_bytes(self, url: str) -> bytes:
        return self.get_document(url)[0]

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8-sig", errors="replace")

    def get_json(self, url: str):
        try:
            return json.loads(self.get_bytes(url))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise OmniError("Site returned invalid JSON") from exc
