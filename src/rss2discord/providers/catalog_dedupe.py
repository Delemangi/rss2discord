"""Shared keyed product deduplication for catalog scans."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import Literal

from rss2discord.fetch_errors import FeedFetchError

DuplicateMode = Literal["error", "error_if_different", "keep_first"]


def append_unique_products[ProductT, KeyT: Hashable](
    products: list[ProductT],
    seen: dict[KeyT, ProductT],
    page_products: Iterable[ProductT],
    *,
    key: Callable[[ProductT], KeyT],
    label: str,
    mode: DuplicateMode = "error",
    cause: str = "DuplicateProductId",
    retryable: bool = False,
    on_duplicate: Callable[[ProductT], None] | None = None,
    max_products: int | None = None,
) -> None:
    """Append products once, applying the provider's duplicate policy.

    ``error`` rejects any duplicate, ``error_if_different`` accepts an
    identical repeated product but rejects conflicting data, and
    ``keep_first`` ignores later observations.
    """
    for product in page_products:
        product_key = key(product)
        existing = seen.get(product_key)
        if existing is None:
            if max_products is not None and len(products) >= max_products:
                raise FeedFetchError(label, "ProductLimitExceeded")
            seen[product_key] = product
            products.append(product)
            continue
        if on_duplicate is not None:
            on_duplicate(product)
        if mode == "keep_first":
            continue
        if mode == "error_if_different" and existing == product:
            continue
        raise FeedFetchError(label, cause, retryable=retryable)
