from decimal import Decimal

from rss2discord.models import EntryData, PriceSnapshot, SourceMetric
from rss2discord.price_amount import discount_percentage
from rss2discord.price_notifications import entry_from_json, entry_to_json
from rss2discord.transports.price_monitor.comparison import price_change_percentage
from rss2discord.transports.price_monitor.presentation import (
    with_price_change_percentage,
)
from tests.discord_components_helpers import get_text_display_contents, make_message


def _snapshot(amount: int, currency: str = "MKD") -> PriceSnapshot:
    return PriceSnapshot(
        feed_id="feed",
        product_id="1",
        amount=Decimal(amount),
        formatted=f"{amount} {currency}",
        currency=currency,
    )


def test_price_change_percentage_reports_signed_change() -> None:
    assert price_change_percentage(_snapshot(100), _snapshot(90)) == "-10.0%"
    assert price_change_percentage(_snapshot(100), _snapshot(120)) == "+20.0%"


def test_price_change_percentage_skips_incomparable_prices() -> None:
    assert price_change_percentage(_snapshot(100), _snapshot(90, "EUR")) is None
    assert price_change_percentage(_snapshot(0), _snapshot(90)) is None


def test_discount_percentage_reports_savings() -> None:
    assert discount_percentage(Decimal(100), Decimal(80)) == "20.0%"
    assert discount_percentage(Decimal(100), Decimal(100)) is None
    assert discount_percentage(Decimal(0), Decimal(0)) is None


def test_with_price_change_percentage_sets_entry_field() -> None:
    message = make_message()

    updated = with_price_change_percentage(
        message,
        _snapshot(100),
        _snapshot(90),
    )

    assert updated.entry.price_change_percentage == "-10.0%"


def test_price_change_percentage_survives_outbox_round_trip() -> None:
    entry = EntryData(
        title="Item",
        link="https://example.test/item",
        description="",
        author="",
        timestamp=None,
        price_change_percentage="-10.0%",
    )

    restored = entry_from_json(entry_to_json(entry))

    assert restored.price_change_percentage == "-10.0%"


def test_components_render_price_change_percentage_as_supporting_metric() -> None:
    entry = EntryData(
        title="Item",
        link="https://example.test/item",
        description="",
        author="",
        timestamp=None,
        source_metrics=(
            SourceMetric("Price", "90 MKD"),
            SourceMetric("Previous", "100 MKD", prior=True),
        ),
        price_change_percentage="-10.0%",
    )

    contents = get_text_display_contents(make_message(entry=entry))

    assert any("Change: -10.0%" in content for content in contents)


def test_discount_percentage_survives_outbox_round_trip() -> None:
    entry = EntryData(
        title="Item",
        link="https://example.test/item",
        description="",
        author="",
        timestamp=None,
        discount_percentage="20.0%",
    )

    restored = entry_from_json(entry_to_json(entry))

    assert restored.discount_percentage == "20.0%"


def test_components_render_discount_percentage_as_supporting_metric() -> None:
    entry = EntryData(
        title="Item",
        link="https://example.test/item",
        description="",
        author="",
        timestamp=None,
        source_metrics=(
            SourceMetric("Price", "80 MKD"),
            SourceMetric("Original", "100 MKD"),
        ),
        discount_percentage="20.0%",
    )

    contents = get_text_display_contents(make_message(entry=entry))

    assert any("Discount: 20.0%" in content for content in contents)
