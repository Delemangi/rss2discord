"""Configuration defaults and validation for per-feed price alert filters."""

from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.configuration import FeedConfig, load_config
from tests.configuration_helpers import write_config


def make_feed(**overrides: object) -> FeedConfig:
    values: dict[str, object] = {
        "id": "shop",
        "url": "https://shop.example.test/catalog",
        "webhook": "https://discord.example.test/hook",
        "strategy": "anhoch",
    }
    values.update(overrides)
    return FeedConfig.model_validate(values)


def test_filters_default_to_disabled() -> None:
    feed = make_feed()

    assert feed.price_min_change_percentage == 0
    assert feed.price_min_change_amount is None
    assert feed.price_ignore_increases is False


def test_price_strategy_accepts_all_filters() -> None:
    feed = make_feed(
        price_min_change_percentage=5,
        price_min_change_amount=Decimal("99.95"),
        price_ignore_increases=True,
    )

    assert feed.price_min_change_percentage == 5
    assert feed.price_min_change_amount == Decimal("99.95")
    assert feed.price_ignore_increases is True


def test_zero_amount_is_allowed_and_does_not_enable_the_filter_requirement() -> None:
    feed = make_feed(price_min_change_amount=0)

    assert feed.price_min_change_amount == Decimal(0)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("price_min_change_percentage", 5),
        ("price_min_change_amount", 10),
        ("price_ignore_increases", True),
    ],
)
def test_non_price_strategy_rejects_filters(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError, match=f"{field_name} requires"):
        make_feed(strategy="rss", **{field_name: value})


def test_rejection_message_names_supported_strategies() -> None:
    with pytest.raises(ValidationError, match="anhoch"):
        make_feed(strategy="rss", price_ignore_increases=True)


@pytest.mark.parametrize("value", [-0.1, -1, -100])
def test_negative_percentage_is_rejected(value: float) -> None:
    with pytest.raises(ValidationError):
        make_feed(price_min_change_percentage=value)


@pytest.mark.parametrize("value", ["-.inf", ".inf", ".nan"])
def test_non_finite_percentage_is_rejected(value: str) -> None:
    with pytest.raises(ValidationError):
        make_feed(price_min_change_percentage=value)


@pytest.mark.parametrize("value", [-1, "-0.01"])
def test_negative_amount_is_rejected(value: int | str) -> None:
    with pytest.raises(ValidationError):
        make_feed(price_min_change_amount=value)


def test_unknown_filter_field_is_still_forbidden() -> None:
    with pytest.raises(ValidationError):
        make_feed(price_min_change_percent=5)


def test_load_config_reads_filters_from_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: shop\n"
        "    url: https://shop.example.test/catalog\n"
        "    webhook: https://discord.example.test/hook\n"
        "    strategy: anhoch\n"
        "    price_min_change_percentage: 5\n"
        "    price_min_change_amount: 100\n"
        "    price_ignore_increases: true\n",
    )

    feed = load_config(config_path).feeds[0]

    assert feed.price_min_change_percentage == 5
    assert feed.price_min_change_amount == Decimal(100)
    assert feed.price_ignore_increases is True
