"""Per-scan price observability evidence."""

import logging
import re
import time
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceSnapshot
from rss2discord.transports.price_monitor import (
    PriceObservation,
    current_scan_started_at,
    price_scan_timing,
    run_price_scan,
)
from tests.setec_price_monitor_helpers import RecordingSender
from tests.test_price_scan_pipeline import (
    dependencies,
    feed,
    message_for,
    seed,
    snapshot,
)

SCAN_LOGGER = "rss2discord.transports.price_monitor.scan"


def unavailable_snapshot(product_id: str, amount: int) -> PriceSnapshot:
    return PriceSnapshot(
        "feed",
        product_id,
        Decimal(amount),
        f"{amount} MKD",
        "MKD",
        available=False,
    )


def run_scan(
    store: DeliveryStore,
    sender: RecordingSender,
    observations: list[PriceObservation[str]],
    *,
    configured_feed: FeedConfig | None = None,
    limit: int = 100,
    started_at: float | None = None,
) -> None:
    run_price_scan(
        feed=configured_feed or feed(),
        dependencies=dependencies(store, sender),
        provider="test",
        label="Test",
        observations=observations,
        message_for=message_for,
        snapshot_limit=limit,
        started_at=started_at,
    )


def test_scan_log_counts_alerts_restocks_filters_and_unavailable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given one delivered alert, one filtered change, one restock, and one
    # out-of-stock price move
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(
            store,
            (
                snapshot("alert", 1000),
                snapshot("filtered", 1000),
                PriceSnapshot(
                    "feed",
                    "stock",
                    Decimal(1000),
                    "1000 MKD",
                    "MKD",
                    available=False,
                ),
                snapshot("out", 1000),
            ),
        )
        sender = RecordingSender(
            [
                DiscordDeliveryResult.DELIVERED,
                DiscordDeliveryResult.DELIVERED,
            ],
        )
        shop = feed().model_copy(
            update={
                "restock_alerts": True,
                "price_min_change_amount": Decimal(10),
            },
        )

        # When
        with caplog.at_level(logging.INFO, logger=SCAN_LOGGER):
            run_scan(
                store,
                sender,
                [
                    PriceObservation("alert", snapshot("alert", 800)),
                    PriceObservation("filtered", snapshot("filtered", 1005)),
                    PriceObservation(
                        "stock",
                        PriceSnapshot(
                            "feed",
                            "stock",
                            Decimal(1000),
                            "1000 MKD",
                            "MKD",
                            available=True,
                        ),
                    ),
                    PriceObservation("out", unavailable_snapshot("out", 900)),
                ],
                configured_feed=shop,
            )

        # Then
        assert "Price scan feed=feed provider=test" in caplog.text
        assert "observations=4" in caplog.text
        assert "alerts=1" in caplog.text
        assert "restocks=1" in caplog.text
        assert "suppressed=1" in caplog.text
        assert "unavailable=1" in caplog.text
        assert "confirmed=2" in caplog.text
        assert "bulk=false" in caplog.text
        assert "outcome=delivered" in caplog.text
        assert "elapsed_ms=" in caplog.text


def test_price_scan_timing_context_sets_and_resets_the_start() -> None:
    # Given no active timing context
    assert current_scan_started_at() is None

    # When / Then
    before = time.perf_counter()
    with price_scan_timing():
        started = current_scan_started_at()
        assert started is not None
        assert started >= before
    assert current_scan_started_at() is None


def test_scan_log_uses_the_provider_start_time(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given a scan that started two seconds before run_price_scan
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, (snapshot("1"),))
        sender = RecordingSender([])

        # When
        with caplog.at_level(logging.INFO, logger=SCAN_LOGGER):
            run_scan(
                store,
                sender,
                [PriceObservation("1", snapshot("1"))],
                started_at=time.perf_counter() - 2.0,
            )

        # Then
        match = re.search(r"elapsed_ms=(\d+)", caplog.text)
        assert match is not None
        assert int(match.group(1)) >= 1_900


def test_scan_log_marks_bulk_delivery(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given more changes than the inline limit
    with DeliveryStore(tmp_path / "state.db") as store:
        ids = tuple(str(index) for index in range(101))
        seed(store, tuple(snapshot(product_id) for product_id in ids))
        sender = RecordingSender([DiscordDeliveryResult.DELIVERED])

        # When
        with caplog.at_level(logging.INFO, logger=SCAN_LOGGER):
            run_scan(
                store,
                sender,
                [
                    PriceObservation(product_id, snapshot(product_id, 90))
                    for product_id in ids
                ],
                limit=500,
            )

        # Then
        assert "observations=101" in caplog.text
        assert "alerts=101" in caplog.text
        assert "confirmed=101" in caplog.text
        assert "bulk=true" in caplog.text
        assert "outcome=delivered" in caplog.text


def test_scan_log_reports_a_fetch_error(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given more retained snapshots than the scan limit
    with DeliveryStore(tmp_path / "state.db") as store:
        seed(store, tuple(snapshot(str(index)) for index in range(3)))
        sender = RecordingSender([])

        # When
        with (
            caplog.at_level(logging.INFO, logger=SCAN_LOGGER),
            pytest.raises(FeedFetchError, match="SnapshotLimitExceeded"),
        ):
            run_scan(store, sender, [], limit=2)

        # Then
        assert "outcome=error" in caplog.text
        assert "error=FeedFetchError" in caplog.text
