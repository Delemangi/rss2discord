"""Shared catalog deduplication policy."""

from dataclasses import dataclass

import pytest

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.catalog_dedupe import append_unique_products


@dataclass(frozen=True)
class Product:
    id: int
    price: int


def test_errors_on_any_duplicate() -> None:
    products: list[Product] = []
    seen: dict[int, Product] = {}
    first = Product(1, 100)

    append_unique_products(
        products,
        seen,
        (first,),
        key=lambda product: product.id,
        label="Shop",
    )

    with pytest.raises(FeedFetchError) as error:
        append_unique_products(
            products,
            seen,
            (first,),
            key=lambda product: product.id,
            label="Shop",
        )
    assert error.value.cause_type == "DuplicateProductId"
    assert products == [first]


def test_error_if_different_accepts_identical_repeat() -> None:
    products: list[Product] = []
    seen: dict[int, Product] = {}
    first = Product(1, 100)

    append_unique_products(
        products,
        seen,
        (first, first),
        key=lambda product: product.id,
        label="Shop",
        mode="error_if_different",
    )

    assert products == [first]


def test_error_if_different_rejects_conflicting_repeat() -> None:
    products: list[Product] = []
    seen: dict[int, Product] = {}

    append_unique_products(
        products,
        seen,
        (Product(1, 100),),
        key=lambda product: product.id,
        label="Shop",
        mode="error_if_different",
        retryable=True,
    )

    with pytest.raises(FeedFetchError) as error:
        append_unique_products(
            products,
            seen,
            (Product(1, 101),),
            key=lambda product: product.id,
            label="Shop",
            mode="error_if_different",
            retryable=True,
        )
    assert error.value.retryable is True


def test_keep_first_logs_and_keeps_original() -> None:
    products: list[Product] = []
    seen: dict[int, Product] = {}
    duplicates: list[int] = []

    append_unique_products(
        products,
        seen,
        (Product(1, 100), Product(1, 101)),
        key=lambda product: product.id,
        label="Shop",
        mode="keep_first",
        on_duplicate=lambda product: duplicates.append(product.id),
    )

    assert products == [Product(1, 100)]
    assert duplicates == [1]


def test_max_products_stops_before_appending() -> None:
    products: list[Product] = []
    seen: dict[int, Product] = {}

    with pytest.raises(FeedFetchError) as error:
        append_unique_products(
            products,
            seen,
            (Product(1, 100),),
            key=lambda product: product.id,
            label="Shop",
            max_products=0,
        )

    assert error.value.cause_type == "ProductLimitExceeded"
    assert products == []
