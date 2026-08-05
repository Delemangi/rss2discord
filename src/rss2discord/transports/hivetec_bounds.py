"""Immutable request and response limits for Hivetec catalog scans."""

from dataclasses import dataclass
from typing import Final

HIVETEC_LABEL: Final = "Hivetec"
HIVETEC_ORIGIN: Final = "https://hivetec.mk"
HIVETEC_SHOP_PATH: Final = "/shop/"
HIVETEC_STORE_API_PATH: Final = "/wp-json/wc/store/v1/products"
HIVETEC_DATES_API_PATH: Final = "/wp-json/wp/v2/product"
HIVETEC_WINDOW_SIZE: Final = 30
HIVETEC_CATALOG_PAGE_SIZE: Final = 100
MAX_HIVETEC_CATALOG_PAGES: Final = 50
MAX_HIVETEC_CATALOG_PRODUCTS: Final = 5_000
MAX_HIVETEC_CATALOG_IMAGES: Final = 20_000
MAX_HIVETEC_CATALOG_CATEGORIES: Final = 20_000
MAX_HIVETEC_RESPONSE_BYTES: Final = 1_048_576
MAX_HIVETEC_DISCOVERY_SCAN_BYTES: Final = 2_097_152
MAX_HIVETEC_CATALOG_SCAN_BYTES: Final = 20_971_520
MAX_HIVETEC_SCAN_SECONDS: Final = 300.0
MAX_HIVETEC_REDIRECTS: Final = 10
HIVETEC_STREAM_CHUNK_BYTES: Final = 65_536
HIVETEC_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)


@dataclass(frozen=True, slots=True)
class HivetecPageRequest:
    """One product API request with its remaining response-byte budget."""

    page: int
    per_page: int
    max_response_bytes: int
