"""Safety limits and source constants for the PROXNet HTML catalog."""

from typing import Final

PROXNET_LABEL: Final = "PROXNet"
PROXNET_HOST: Final = "www.proxnet.mk"
PROXNET_ORIGIN: Final = f"https://{PROXNET_HOST}"
PROXNET_CATEGORY_PATH_PREFIX: Final = "/mk/"
PROXNET_PRODUCTS_PER_PAGE: Final = 60
MAX_PROXNET_PAGES: Final = 20
MAX_PROXNET_LATEST_PAGES: Final = 2
MAX_PROXNET_PRODUCTS: Final = 1_500
MAX_PROXNET_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_PROXNET_SCAN_BYTES: Final = MAX_PROXNET_PAGES * MAX_PROXNET_RESPONSE_BYTES
PROXNET_REQUEST_TIMEOUT: Final = 30.0
PROXNET_INTER_REQUEST_DELAY: Final = 0.25
PROXNET_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
MAX_PROXNET_RETAINED_SNAPSHOTS: Final = 2_000
