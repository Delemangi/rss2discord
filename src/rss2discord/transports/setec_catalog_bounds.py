"""Immutable request and response limits for Setec search scans."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

SETEC_LABEL: Final = "Setec"
SETEC_SEARCH_URL: Final = "https://search.sp.solslab.dev/indexes/products/search"
SETEC_SEARCH_KEY: Final = (
    "c0424dab588b8cbbbe0a4809fc10b5f1c0c7d183b5b28ebe799f3fbf583ab358"
)
SETEC_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"

SETEC_PRICE_FIELD: Final = "variants.calculated_price.calculated_amount"
# Counting facets a single-valued field, because a multi-valued one (such as the
# price of a multi-variant product) is counted once per distinct value rather than
# once per document, which would overstate the catalog total.
SETEC_COUNT_FIELD: Final = "status"
SETEC_CREATED_AT_SORT: Final = "created_at:desc"

SETEC_WINDOW_SIZE: Final = 100
SETEC_SEARCH_PAGE_SIZE: Final = 1_000
SETEC_PRODUCT_LOOKUP_BATCH_SIZE: Final = 200

SETEC_PRICE_PROJECTION: Final = (
    "id",
    "variants.calculated_price.calculated_amount",
    "variants.calculated_price.currency_code",
)
SETEC_DISPLAY_PROJECTION: Final = (
    "id",
    "title",
    "handle",
    "thumbnail",
    "created_at",
    "product_categories.name",
    "variants.calculated_price.calculated_amount",
    "variants.calculated_price.original_amount",
    "variants.calculated_price.currency_code",
)

MAX_SETEC_CATALOG_PRODUCTS: Final = 25_000
MAX_SETEC_SEARCH_REQUESTS: Final = 1_000
MAX_SETEC_BAND_DEPTH: Final = 48
MAX_SETEC_LATEST_RESPONSE_BYTES: Final = 1_048_576
MAX_SETEC_CATALOG_RESPONSE_BYTES: Final = 5_242_880
MAX_SETEC_CATALOG_SCAN_BYTES: Final = 524_288_000
SETEC_STREAM_CHUNK_BYTES: Final = 65_536
MAX_SETEC_REDIRECTS: Final = 10


@dataclass(frozen=True, slots=True)
class SetecSearchRequest:
    """One search query with its per-response and remaining scan byte budgets."""

    body: Mapping[str, object]
    max_single_response_bytes: int
    remaining_scan_response_bytes: int
