"""Safety limits and source constants for the CCCenter HTML catalog."""

from typing import Final

CCCENTER_LABEL: Final = "CCCenter"
CCCENTER_ORIGIN: Final = "https://cccenter.mk"
CCCENTER_FEED_URL: Final = f"{CCCENTER_ORIGIN}/shop/?orderby=date"
CCCENTER_SHOP_PATH: Final = "/shop/"
CCCENTER_PRODUCTS_PER_PAGE: Final = 24
MAX_CCCENTER_PAGES: Final = 12
MAX_CCCENTER_PRODUCTS: Final = 2_000
MAX_CCCENTER_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_CCCENTER_SCAN_BYTES: Final = MAX_CCCENTER_PAGES * MAX_CCCENTER_RESPONSE_BYTES
# Enumeration fetches index pages, not one detail page per product. Allow bounded
# retry overhead while retaining the shared byte and wall-clock budgets.
MAX_CCCENTER_REQUESTS: Final = 3 * MAX_CCCENTER_PAGES
MAX_CCCENTER_SCAN_SECONDS: Final = 300.0
MAX_CCCENTER_PRICE_CHANGES_PER_SCAN: Final = 100
MAX_CCCENTER_RETAINED_SNAPSHOTS: Final = 10_000
CCCENTER_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
