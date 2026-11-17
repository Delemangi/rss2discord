"""Immutable request and response limits for WooCommerce Store API scans."""

from dataclasses import dataclass
from typing import Final

WOOCOMMERCE_LABEL: Final = "WooCommerce"
WOOCOMMERCE_STORE_API_PATH: Final = "/wp-json/wc/store/v1/products"
WOOCOMMERCE_CATEGORIES_API_PATH: Final = "/wp-json/wc/store/v1/products/categories"
WOOCOMMERCE_WINDOW_SIZE: Final = 30
WOOCOMMERCE_PAGE_SIZE: Final = 100
MAX_WOOCOMMERCE_PAGES: Final = 50
MAX_WOOCOMMERCE_PRODUCTS: Final = 5_000
MAX_WOOCOMMERCE_CATEGORY_PAGES: Final = 5
MAX_WOOCOMMERCE_CATEGORIES: Final = 500
MAX_WOOCOMMERCE_RESPONSE_BYTES: Final = 4 * 1024 * 1024
MAX_WOOCOMMERCE_DISCOVERY_SCAN_BYTES: Final = 8 * 1024 * 1024
MAX_WOOCOMMERCE_CATALOG_SCAN_BYTES: Final = 64 * 1024 * 1024
MAX_WOOCOMMERCE_SCAN_SECONDS: Final = 300.0
MAX_WOOCOMMERCE_REDIRECTS: Final = 5
WOOCOMMERCE_USER_AGENT: Final = "Mozilla/5.0 (compatible; rss2discord/0.1)"
WOOCOMMERCE_PRODUCT_FIELDS: Final = (
    "id,name,permalink,sku,prices,images,categories,is_in_stock,type"
)


@dataclass(frozen=True, slots=True)
class WooCommerceShop:
    """One validated HTTPS shop origin and optional category slug."""

    origin: str
    hostname: str
    category_slug: str | None = None


@dataclass(frozen=True, slots=True)
class WooCommercePageRequest:
    """One product API request with its remaining response-byte budget."""

    page: int
    per_page: int
    category_id: int | None
    remaining_scan_bytes: int
