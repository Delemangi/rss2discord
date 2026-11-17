"""Safety limits and source constants for the Ananas Algolia catalog."""

from typing import Final

ANANAS_LABEL: Final = "Ananas"
ANANAS_ORIGIN: Final = "https://ananas.mk"
ANANAS_HOST: Final = "ananas.mk"
ANANAS_IMAGE_HOST: Final = "static.ananas.rs"
ANANAS_FEED_URL: Final = (
    f"{ANANAS_ORIGIN}/kategorii/it-shop/kompjuteri-i-kompjuterska-oprema"
)
ANANAS_COMPONENTS_FEED_URL: Final = (
    f"{ANANAS_ORIGIN}/kategorii/it-shop/kompjuterski-komponenti"
)
ANANAS_CATEGORY_PATH_PREFIX: Final = "/kategorii/"
ANANAS_PAGE_SIZE: Final = 48
ANANAS_ALGOLIA_ENDPOINT: Final = (
    "https://api.ananas.rs/algolia-reverse-proxy/1/indexes/"
    "prod_merchant_inventories_mk_mk/query"
)
ANANAS_ALGOLIA_APPLICATION_ID: Final = "Y1BSBVJ7AC"
ANANAS_ALGOLIA_INDEX: Final = "prod_merchant_inventories_mk_mk"
ANANAS_ALGOLIA_SEARCH_KEY_ENV: Final = "ANANAS_ALGOLIA_SEARCH_KEY"
ANANAS_JSON_CONTENT_TYPE: Final = "application/json"
ANANAS_GRAPHQL_ENDPOINT: Final = "https://api.ananas.rs/graphql-gateway/public"
ANANAS_GRAPHQL_MARKET: Final = "MK"
ANANAS_GRAPHQL_SOURCE_APP: Final = "ananas-customer-app-v2/v2.16.1"
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
MAX_ANANAS_PAGES: Final = 250
MAX_ANANAS_PRODUCTS: Final = 12_000
MAX_ANANAS_RESPONSE_BYTES: Final = 2 * 1024 * 1024
MAX_ANANAS_CATEGORY_RESPONSE_BYTES: Final = 2 * 1024 * 1024
# Full scans re-read page zero to detect index drift, so budget one extra page.
MAX_ANANAS_SCAN_BYTES: Final = (MAX_ANANAS_PAGES + 1) * MAX_ANANAS_RESPONSE_BYTES
# Allow bounded retry overhead while retaining the shared byte and wall-clock
# budgets. A full scan includes one final page-zero drift recheck.
MAX_ANANAS_REQUESTS: Final = 3 * (MAX_ANANAS_PAGES + 1)
MAX_ANANAS_SCAN_SECONDS: Final = 600.0
MAX_ANANAS_RETAINED_SNAPSHOTS: Final = 10_000
# The live marketplace can change totals and page boundaries during a scan.
# Accept bounded drift, but require nearly complete inventory coverage.
ANANAS_MIN_COVERAGE_RATIO: Final = 0.98
ANANAS_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
