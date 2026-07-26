"""Immutable request and response limits for Setec catalog scans."""

from dataclasses import dataclass
from typing import Final

SETEC_WINDOW_SIZE: Final = 30
SETEC_LABEL: Final = "Setec"
SETEC_API_PATH: Final = "/api/medusa/products/list"
SETEC_REGION_ID: Final = "mk"
SETEC_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
SETEC_CATALOG_PAGE_SIZE: Final = 100
MAX_SETEC_CATALOG_PAGES: Final = 100
MAX_SETEC_CATALOG_PRODUCTS: Final = 10_000
MAX_SETEC_LATEST_RESPONSE_BYTES: Final = 1_048_576
MAX_SETEC_CATALOG_RESPONSE_BYTES: Final = 2_097_152
MAX_SETEC_CATALOG_SCAN_BYTES: Final = 209_715_200
SETEC_STREAM_CHUNK_BYTES: Final = 65_536
MAX_SETEC_REDIRECTS: Final = 10


@dataclass(frozen=True, slots=True)
class CatalogPageRequest:
    """One catalog page request with its remaining global response-byte budget."""

    limit: int
    offset: int
    max_single_response_bytes: int
    remaining_scan_response_bytes: int
