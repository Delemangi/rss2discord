import pytest
from pydantic import JsonValue, ValidationError

from rss2discord.transports.ddstore_models import DDStoreProduct
from tests.ddstore_helpers import product_payload


def categories(count: int) -> list[JsonValue]:
    return [{"name": f"Category {index}"} for index in range(count)]


def test_product_accepts_sixty_four_categories() -> None:
    # Given
    payload = product_payload("product")
    payload["categories"] = categories(64)

    # When
    product = DDStoreProduct.model_validate(payload)

    # Then
    assert product.categories is not None
    assert len(product.categories) == 64


def test_product_rejects_more_than_sixty_four_categories() -> None:
    # Given
    payload = product_payload("product")
    payload["categories"] = categories(65)
    categories_payload = payload["categories"]
    assert isinstance(categories_payload, list)
    categories_payload[0] = {"name": []}

    # When / Then
    with pytest.raises(ValidationError) as error:
        DDStoreProduct.model_validate(payload)

    assert [detail["loc"] for detail in error.value.errors()] == [("categories",)]
    assert "too many product categories" in str(error.value.errors()[0]["ctx"])
