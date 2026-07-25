import logging
from decimal import Decimal
from pathlib import Path

import pytest
import requests

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    SQLiteRetryInterruptedError,
    SQLiteRetryPolicy,
)
from rss2discord.transports.anhoch_price_monitor import (
    AnhochPriceMonitor,
    AnhochPriceMonitorDependencies,
    PriceAlertDelivery,
)
from tests.anhoch_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    RetryingFailureCatalog,
    RetrySleepAdapter,
    busy_database_error,
    make_feed,
    make_monitor,
    make_product,
    snapshots_by_product,
)


def test_fetch_retry_interruption_stops_before_alerting_and_logs_no_secrets(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    feed = make_feed()
    sender = RecordingSender([])

    def request_shutdown(_seconds: float) -> bool:
        return False

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            feed,
            RetryingFailureCatalog(),
            store,
            sender,
            sleep=request_shutdown,
        )

        # When / Then
        with caplog.at_level(logging.ERROR), pytest.raises(FeedFetchInterruptedError):
            monitor.scan()

    assert sender.messages == []
    assert feed.url not in caplog.text
    assert feed.webhook not in caplog.text


def test_persistence_retry_interruption_stops_before_later_alerts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_before = make_product(1, amount="100", formatted="100 den")
    second_before = make_product(2, amount="200", formatted="200 den")
    first_after = make_product(1, amount="90", formatted="90 den")
    second_after = make_product(2, amount="190", formatted="190 den")
    sender = RecordingSender([True, True])

    def request_shutdown(_seconds: float) -> bool:
        return False

    with DeliveryStore(tmp_path / "state.db") as store:
        baseline_monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_before, second_before)]),
            store,
            RecordingSender([]),
        )
        baseline_monitor.scan()

        def always_busy(snapshot: PriceSnapshot) -> None:
            del snapshot
            raise busy_database_error()

        monkeypatch.setattr(store, "upsert_price_snapshot", always_busy)
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_after, second_after)]),
            store,
            sender,
            sleep=request_shutdown,
        )

        # When / Then
        with pytest.raises(SQLiteRetryInterruptedError):
            monitor.scan()

    assert [message.entry.title for message in sender.messages] == ["Product 1"]


def test_retry_interruption_stops_before_later_alert_and_preserves_prior_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_before = make_product(1, amount="100", formatted="100 den")
    interrupted_before = make_product(2, amount="200", formatted="200 den")
    later_before = make_product(3, amount="300", formatted="300 den")
    first_after = make_product(1, amount="90", formatted="90 den")
    interrupted_after = make_product(2, amount="190", formatted="190 den")
    later_after = make_product(3, amount="290", formatted="290 den")
    session = requests.Session()
    attempts = 0
    shutdown_requested = False

    def post(url: str, **kwargs: object) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        if attempts == 1:
            response = requests.Response()
            response.status_code = 200
            response.url = "https://discord.example.test/webhooks/id/hidden"
            response._content = b'{"id":"accepted"}'
            return response
        if attempts == 2:
            raise requests.ConnectionError("connection reset")
        raise AssertionError("later price alert must not be sent")

    def request_shutdown(_seconds: float) -> bool:
        nonlocal shutdown_requested
        shutdown_requested = True
        return False

    monkeypatch.setattr(session, "post", post)

    with DeliveryStore(tmp_path / "state.db") as store:
        baseline_monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_before, interrupted_before, later_before)]),
            store,
            RecordingSender([]),
        )
        baseline_monitor.scan()
        monitor = AnhochPriceMonitor(
            make_feed(),
            AnhochPriceMonitorDependencies(
                catalog=CatalogStub([(first_after, interrupted_after, later_after)]),
                snapshots=store,
                sender=DiscordWebhookClient(session),
                fetch_retry_policy=FetchRetryPolicy(
                    sleep=RetrySleepAdapter(request_shutdown),
                    on_retry=lambda error, delay: None,
                ),
                sqlite_retry_policy=SQLiteRetryPolicy(
                    sleep=RetrySleepAdapter(request_shutdown),
                    on_retry=lambda error, delay: None,
                ),
                delivery=PriceAlertDelivery(
                    sleep=request_shutdown,
                    delay_between_posts=0,
                    is_shutdown_requested=lambda: shutdown_requested,
                ),
            ),
        )

        # When
        monitor.scan()

        # Then
        snapshots = snapshots_by_product(store)

    assert attempts == 2
    assert snapshots[1].amount == Decimal(90)
    assert snapshots[2].amount == Decimal(200)
    assert snapshots[3].amount == Decimal(300)


def test_retry_interruption_stops_before_the_next_changed_product(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    first_before = make_product(1, amount="100", formatted="100 den")
    second_before = make_product(2, amount="200", formatted="200 den")
    first_after = make_product(1, amount="90", formatted="90 den")
    second_after = make_product(2, amount="190", formatted="190 den")
    session = requests.Session()
    attempts = 0
    shutdown_requested = False

    def post(url: str, **kwargs: object) -> requests.Response:
        nonlocal attempts
        del url, kwargs
        attempts += 1
        if attempts > 1:
            raise AssertionError("next changed product must not be sent")
        raise requests.ConnectionError("connection reset")

    def request_shutdown(_seconds: float) -> bool:
        nonlocal shutdown_requested
        shutdown_requested = True
        return False

    monkeypatch.setattr(session, "post", post)

    with DeliveryStore(tmp_path / "state.db") as store:
        baseline_monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_before, second_before)]),
            store,
            RecordingSender([]),
        )
        baseline_monitor.scan()
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(first_after, second_after)]),
            store,
            DiscordWebhookClient(session),
            sleep=request_shutdown,
            is_shutdown_requested=lambda: shutdown_requested,
        )

        # When
        monitor.scan()

        # Then
        snapshots = snapshots_by_product(store)

    assert attempts == 1
    assert snapshots[1].amount == Decimal(100)
    assert snapshots[2].amount == Decimal(200)
