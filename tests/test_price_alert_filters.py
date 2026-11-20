"""Pure per-feed price alert filter semantics."""

from decimal import Decimal

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.models import PriceChangeRecord, PriceSnapshot
from rss2discord.transports.price_monitor.filters import split_price_alert_changes


def make_feed(**overrides: object) -> FeedConfig:
    values: dict[str, object] = {
        "id": "shop",
        "url": "https://shop.example.test/catalog",
        "webhook": "https://discord.example.test/hook",
        "strategy": "anhoch",
    }
    values.update(overrides)
    return FeedConfig.model_validate(values)


def make_snapshot(
    amount: int | str | Decimal,
    *,
    product_id: str = "1",
    currency: str = "MKD",
) -> PriceSnapshot:
    decimal_amount = Decimal(str(amount))
    return PriceSnapshot(
        "shop",
        product_id,
        decimal_amount,
        f"{decimal_amount} {currency}",
        currency,
    )


def make_record(
    previous: int | str | Decimal = 1000,
    current: int | str | Decimal = 900,
    *,
    currency: str = "MKD",
    current_currency: str = "MKD",
) -> PriceChangeRecord:
    return PriceChangeRecord(
        "1",
        make_snapshot(previous, currency=currency),
        make_snapshot(current, currency=current_currency),
    )


def assert_alerting(feed: FeedConfig, record: PriceChangeRecord) -> None:
    alerting, suppressed = split_price_alert_changes((record,), feed)
    assert alerting == (record,)
    assert suppressed == ()


def assert_suppressed(feed: FeedConfig, record: PriceChangeRecord) -> None:
    alerting, suppressed = split_price_alert_changes((record,), feed)
    assert alerting == ()
    assert suppressed == (record,)


@pytest.mark.parametrize(
    ("current", "suppressed"),
    [("1040", True), ("1050", False), ("1060", False)],
)
def test_percentage_threshold_below_at_and_above(
    current: str,
    *,
    suppressed: bool,
) -> None:
    # Given a 5% minimum on a previous amount of 1000
    feed = make_feed(price_min_change_percentage=5)
    record = make_record(1000, current)

    # When / Then
    if suppressed:
        assert_suppressed(feed, record)
    else:
        assert_alerting(feed, record)


@pytest.mark.parametrize(
    ("current", "suppressed"),
    [("901", True), ("900", False), ("899", False)],
)
def test_absolute_threshold_below_at_and_above(
    current: str,
    *,
    suppressed: bool,
) -> None:
    # Given a minimum absolute change of 100
    feed = make_feed(price_min_change_amount=Decimal(100))
    record = make_record(1000, current)

    # When / Then
    if suppressed:
        assert_suppressed(feed, record)
    else:
        assert_alerting(feed, record)


def test_ignore_increases_suppresses_rise_but_allows_drop() -> None:
    feed = make_feed(price_ignore_increases=True)

    assert_suppressed(feed, make_record(1000, 1100))
    assert_alerting(feed, make_record(1000, 900))


def test_both_thresholds_must_pass_for_a_change_to_alert() -> None:
    # Given both filters configured, suppression needs only one failure
    feed = make_feed(
        price_min_change_percentage=5,
        price_min_change_amount=Decimal(100),
    )

    # 4% and 40 -> fails both
    assert_suppressed(feed, make_record(1000, 1040))
    # 5% but only 50 -> passes the percentage, fails the amount
    assert_suppressed(feed, make_record(1000, 1050))
    # 10% and 100 -> passes both
    assert_alerting(feed, make_record(1000, 1100))


def test_currency_change_is_never_suppressed() -> None:
    feed = make_feed(
        price_min_change_percentage=50,
        price_min_change_amount=Decimal(500),
        price_ignore_increases=True,
    )

    assert_alerting(feed, make_record(1000, 1000, current_currency="EUR"))


@pytest.mark.parametrize("previous", [0, -5])
def test_non_positive_previous_amount_is_never_suppressed(previous: int) -> None:
    feed = make_feed(
        price_min_change_percentage=50,
        price_min_change_amount=Decimal(500),
    )

    assert_alerting(feed, make_record(previous, 10))


def test_defaults_keep_every_change_alerting() -> None:
    feed = make_feed()

    assert_alerting(feed, make_record(1000, 1001))
    assert_alerting(feed, make_record(1000, 999))
    assert_alerting(feed, make_record(1000, 5000))


def test_split_preserves_input_order_within_each_group() -> None:
    feed = make_feed(price_min_change_amount=Decimal(100))
    tiny_drop = make_record(1000, 950)
    big_drop = make_record(1000, 800)
    tiny_rise = make_record(1000, 1050)
    big_rise = make_record(1000, 1200)

    alerting, suppressed = split_price_alert_changes(
        (tiny_drop, big_drop, tiny_rise, big_rise),
        feed,
    )

    assert alerting == (big_drop, big_rise)
    assert suppressed == (tiny_drop, tiny_rise)
