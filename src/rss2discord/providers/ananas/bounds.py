"""Safety limits and source constants for the Ananas Algolia catalog."""

from types import MappingProxyType
from typing import Final

ANANAS_LABEL: Final = "Ananas"
ANANAS_ORIGIN: Final = "https://ananas.mk"
ANANAS_HOST: Final = "ananas.mk"
ANANAS_IMAGE_HOST: Final = "static.ananas.rs"
ANANAS_FEED_URL: Final = (
    f"{ANANAS_ORIGIN}/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema"
)
# Category filters are keyed by the only supported public category URL so more
# categories can be added without changing the catalog or strategy contracts.
ANANAS_CATEGORY_FILTERS: Final = MappingProxyType(
    {
        ANANAS_FEED_URL: (
            'product.categories.lvl1:"IT Shop > Компјутери и компјутерска опрема"'
        ),
    },
)
ANANAS_PAGE_SIZE: Final = 48
ANANAS_ALGOLIA_ENDPOINT: Final = (
    "https://api.ananas.rs/algolia-reverse-proxy/1/indexes/"
    "prod_merchant_inventories_mk_mk/query"
)
ANANAS_ALGOLIA_APPLICATION_ID: Final = "Y1BSBVJ7AC"
ANANAS_ALGOLIA_INDEX: Final = "prod_merchant_inventories_mk_mk"
ANANAS_ALGOLIA_SEARCH_KEY_ENV: Final = "ANANAS_ALGOLIA_SEARCH_KEY"
ANANAS_JSON_CONTENT_TYPE: Final = "application/json"
ANANAS_ATTRIBUTES_TO_RETRIEVE: Final = (
    "objectID",
    "price",
    "basePrice",
    "discountPercentage",
    "onSale",
    "onStock",
    "available",
    "firstPublishedTimestamp",
    "merchant.displayName",
    "product.name",
    "product.slug",
    "product.brand",
    "product.coverImageUrl",
    "product.categoryNames",
    "product.hasVariations",
    "product.newProduct",
)
ANANAS_LATEST_LOOKBACK_MS: Final = 7 * 24 * 60 * 60 * 1000
MAX_ANANAS_PAGES: Final = 100
MAX_ANANAS_PRODUCTS: Final = 10_000
MAX_ANANAS_RESPONSE_BYTES: Final = 2 * 1024 * 1024
# Full scans re-read page zero to detect index drift, so budget one extra page.
MAX_ANANAS_SCAN_BYTES: Final = (MAX_ANANAS_PAGES + 1) * MAX_ANANAS_RESPONSE_BYTES
# Allow bounded retry overhead while retaining the shared byte and wall-clock
# budgets. A full scan includes one final page-zero drift recheck.
MAX_ANANAS_REQUESTS: Final = 3 * (MAX_ANANAS_PAGES + 1)
MAX_ANANAS_SCAN_SECONDS: Final = 300.0
MAX_ANANAS_RETAINED_SNAPSHOTS: Final = 10_000
ANANAS_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
