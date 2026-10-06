from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from rss2discord.models import EntryId, SourceMetric
from rss2discord.providers.neksio.catalog import NeksioCatalogClient
from rss2discord.providers.neksio.models import NeksioProduct, NeksioProductCard
from rss2discord.providers.neksio.strategy import NeksioStrategy
from tests.neksio_helpers import product_card

CATALOG_URL = "https://g.store.neksio.mk/"
OBSERVED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def make_product(
    product_id: int = 2077,
    *,
    old_formatted_price: str | None = "1.500 ден.",
) -> NeksioProduct:
    return NeksioProduct(
        product_id=product_id,
        product_name="Example GPU",
        product_code="GPU-2077",
        category="Components",
        subcategory="Graphics",
        manufacturer="Example",
        price_with_tax=Decimal(1200),
        formatted_price="1.200 ден.",
        old_formatted_price=old_formatted_price,
        image_path="/images/gpu-2077.png",
        stock_quantity=7,
        observed_at=OBSERVED_AT,
    )


def test_neksio_strategy_fetches_catalog_and_returns_source_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    product = make_product()

    def fetch_catalog(
        client: NeksioCatalogClient,
        url: str,
        *,
        is_shutdown_requested: Callable[[], bool] | None = None,
    ) -> tuple[NeksioProduct, ...]:
        del client, is_shutdown_requested
        assert url == CATALOG_URL
        return (product,)

    monkeypatch.setattr(NeksioCatalogClient, "fetch_catalog", fetch_catalog)

    # When
    entries, source_title = NeksioStrategy().fetch_entries(CATALOG_URL)

    # Then
    assert entries == [product]
    assert source_title == "Neksio"


def test_neksio_strategy_maps_product_entry_data() -> None:
    # Given
    product = make_product()
    strategy = NeksioStrategy()

    # When
    entry_id = strategy.get_entry_id(product)
    entry_data = strategy.get_entry_data(product)

    # Then
    assert strategy.seed_existing_on_first_fetch
    assert entry_id == EntryId("2077")
    assert entry_data.title == "Example GPU"
    assert entry_data.link == "https://g.store.neksio.mk/Product/Details/2077"
    assert entry_data.description == ""
    assert entry_data.author == ""
    assert entry_data.timestamp == OBSERVED_AT.isoformat()
    assert entry_data.image_url == "https://g.store.neksio.mk/images/gpu-2077.png"
    assert entry_data.categories == ("Components", "Graphics")
    assert entry_data.source_metrics == (
        SourceMetric(label="Price", value="1.200 ден."),
        SourceMetric(label="Original", value="1.500 ден."),
        SourceMetric(label="Product code", value="GPU-2077"),
        SourceMetric(label="Manufacturer", value="Example"),
        SourceMetric(label="Stock", value="7"),
    )


def test_neksio_strategy_omits_missing_old_price_metric() -> None:
    # Given
    product = make_product(old_formatted_price=None)

    # When
    entry_data = NeksioStrategy().get_entry_data(product)

    # Then
    assert entry_data.source_metrics == (
        SourceMetric(label="Price", value="1.200 ден."),
        SourceMetric(label="Product code", value="GPU-2077"),
        SourceMetric(label="Manufacturer", value="Example"),
        SourceMetric(label="Stock", value="7"),
    )


def test_neksio_strategy_omits_empty_subcategory() -> None:
    # Given
    product = make_product().model_copy(update={"subcategory": ""})

    # When
    entry_data = NeksioStrategy().get_entry_data(product)

    # Then
    assert entry_data.categories == ("Components",)


@pytest.mark.parametrize(
    ("raw_quantity", "expected_stock"),
    [(-2, 0), (-1, 0), (-10, 0), (0, 0), (7, 7)],
)
def test_neksio_card_normalizes_negative_stock_and_keeps_product_data(
    raw_quantity: int,
    expected_stock: int,
) -> None:
    # Given
    card = product_card(19141)
    card["quantity"] = raw_quantity

    # When
    product = NeksioProductCard.model_validate(card).observe(OBSERVED_AT)
    entry_data = NeksioStrategy().get_entry_data(product)

    # Then
    assert product.product_id == 19141
    assert product.price_with_tax == Decimal(1_200)
    assert product.stock_quantity == expected_stock
    assert NeksioStrategy().get_entry_id(product) == EntryId("19141")
    assert entry_data.source_metrics == (
        SourceMetric(label="Price", value="1.200 ден."),
        SourceMetric(label="Product code", value="CODE-19141"),
        SourceMetric(label="Manufacturer", value="Example"),
        SourceMetric(label="Stock", value=str(expected_stock)),
    )


@pytest.mark.parametrize("quantity", ["not-an-integer", 1.5])
def test_neksio_card_still_rejects_non_integer_stock(quantity: object) -> None:
    payload: dict[str, object] = dict(product_card(19141))
    payload["quantity"] = quantity

    with pytest.raises(ValidationError):
        NeksioProductCard.model_validate(payload)
