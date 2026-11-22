"""Hetzner Cloud availability provider behavior."""

import json
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.models import PriceDirection
from rss2discord.providers.hetzner_cloud.client import (
    HetznerCloudCatalogClient,
    validate_hetzner_cloud_url,
)
from rss2discord.providers.hetzner_cloud.models import HetznerCloudOffer
from rss2discord.providers.hetzner_cloud.prices import HetznerCloudPriceMonitor
from rss2discord.providers.hetzner_cloud.strategy import HetznerCloudStrategy
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender

FEED_URL = "https://api.hetzner.cloud/v1/server_types?locations=fsn1,nbg1,hel1"
FEED_URL_FSN1 = "https://api.hetzner.cloud/v1/server_types?locations=fsn1"


def money(gross: str) -> dict[str, str]:
    return {"gross": gross, "net": gross}


def server_type(
    *,
    name: str = "cax11",
    available: bool = True,
    price: str = "5.99",
) -> dict[str, object]:
    return {
        "id": 1,
        "name": name,
        "description": name.upper(),
        "cores": 2,
        "memory": 4.0,
        "disk": 40,
        "architecture": "arm",
        "cpu_type": "shared",
        "storage_type": "local",
        "deprecation": None,
        "locations": [
            {"id": 1, "name": "fsn1", "available": available},
            {"id": 2, "name": "nbg1", "available": False},
            {"id": 3, "name": "hel1", "available": False},
            {"id": 4, "name": "ash", "available": True},
        ],
        "prices": [
            {"location": "fsn1", "price_monthly": money(price)},
            {"location": "nbg1", "price_monthly": money(price)},
            {"location": "hel1", "price_monthly": money("6.49")},
            {"location": "ash", "price_monthly": money("7.99")},
        ],
    }


def payload(*server_types: dict[str, object]) -> dict[str, object]:
    return {"server_types": list(server_types)}


def client_with(
    data: dict[str, object],
    *,
    token: str | None = "test-token",  # noqa: S107
) -> HetznerCloudCatalogClient:
    return HetznerCloudCatalogClient(
        fetch=lambda _url, _token: json.dumps(data).encode(),
        token=token,
    )


def offers_for(
    *,
    available: bool,
    price: str = "5.99",
    url: str = FEED_URL_FSN1,
) -> tuple[HetznerCloudOffer, ...]:
    return client_with(
        payload(server_type(available=available, price=price)),
    ).fetch_catalog(
        url,
    )


class CatalogStub:
    def __init__(self, batches: list[tuple[HetznerCloudOffer, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[HetznerCloudOffer, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def make_monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> HetznerCloudPriceMonitor:
    return HetznerCloudPriceMonitor(
        FeedConfig(
            id="hetzner-cloud",
            name="Hetzner Cloud Availability",
            url=FEED_URL,
            webhook="https://discord.test/webhooks/id/token",
            strategy="hetzner_cloud",
            price_check_interval=120,
            restock_alerts=True,
            price_ignore_increases=True,
        ),
        PriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


def test_catalog_parses_offers_and_availability() -> None:
    offers = client_with(payload(server_type())).fetch_catalog(FEED_URL)

    assert len(offers) == 3
    by_location = {offer.location.name: offer for offer in offers}
    assert set(by_location) == {"fsn1", "nbg1", "hel1"}
    assert by_location["fsn1"].location.available is True
    assert by_location["nbg1"].location.available is False
    assert by_location["fsn1"].price == Decimal("5.99")
    assert by_location["hel1"].price == Decimal("6.49")
    assert by_location["fsn1"].server_type.name == "cax11"


def test_catalog_filters_server_types() -> None:
    data = payload(
        server_type(name="cax11"),
        server_type(name="cx23"),
    )

    offers = client_with(data).fetch_catalog(
        f"{FEED_URL}&server_types=cx23",
    )

    assert [offer.server_type.name for offer in offers] == ["cx23"] * 3


def test_catalog_rejects_unknown_empty_and_malformed_filters() -> None:
    data = payload(server_type())
    for query in (
        "?only_available=true",
        "?locations=",
        "?locations=fsn1,,nbg1",
        "?locations=fsn1&locations=nbg1",
        "?locations=Bogus",
    ):
        with pytest.raises(FeedFetchError) as error:
            client_with(data).fetch_catalog(
                f"https://api.hetzner.cloud/v1/server_types{query}",
            )
        assert error.value.cause_type == "InvalidUrl"


@pytest.mark.parametrize(
    "url",
    [
        "http://api.hetzner.cloud/v1/server_types",
        "https://example.com/v1/server_types",
        "https://api.hetzner.cloud/v1/other",
        "https://api.hetzner.cloud/v1/server_types#fragment",
        "https://user@api.hetzner.cloud/v1/server_types",
    ],
)
def test_catalog_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        validate_hetzner_cloud_url(url)

    assert error.value.cause_type == "InvalidUrl"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"server_types": []},
        {"server_types": "nope"},
        {"server_types": [{"name": 1}]},
    ],
)
def test_catalog_rejects_invalid_responses(data: dict[str, object]) -> None:
    with pytest.raises(FeedFetchError) as error:
        client_with(data).fetch_catalog(FEED_URL)

    assert error.value.cause_type == "InvalidResponse"


def test_catalog_requires_token() -> None:
    with pytest.raises(FeedFetchError) as error:
        HetznerCloudCatalogClient(
            fetch=lambda _url, _token: b"{}",
            token="",
            environment={},
        ).fetch_catalog(FEED_URL)

    assert error.value.cause_type == "MissingToken"


def test_catalog_reads_token_from_environment() -> None:
    offers = HetznerCloudCatalogClient(
        fetch=lambda _url, token: (
            json.dumps(payload(server_type())).encode()
            if token == "from-env"  # noqa: S105
            else b"{}"
        ),
        environment={"HETZNER_CLOUD_TOKEN": "from-env"},
    ).fetch_catalog(FEED_URL)

    assert len(offers) == 3


def test_price_monitor_alerts_on_restock(tmp_path: Path) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    offers_for(available=False),
                    offers_for(available=True),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.title == "cax11 · Falkenstein (fsn1)"
        assert message.entry.link == "https://console.hetzner.cloud/"
        assert message.entry.restocked is True
        assert message.entry.price_direction is None
        assert [metric.label for metric in message.entry.source_metrics] == [
            "Price",
            "Previous",
            "Location",
            "Type",
            "CPU",
            "RAM",
            "Disk",
            "Architecture",
        ]
        assert message.entry.source_metrics[0].value == "€5.99/mo"
        assert message.entry.source_metrics[2].value == "Falkenstein (fsn1)"
        snapshots = store.load_price_snapshots("hetzner-cloud")
        assert len(snapshots) == 1
        assert snapshots[0].product_id == "cax11:fsn1"
        assert snapshots[0].available is True


def test_price_monitor_does_not_alert_when_stock_disappears(
    tmp_path: Path,
) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    offers_for(available=True),
                    offers_for(available=False),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()

        assert sender.messages == []
        assert store.load_price_snapshots("hetzner-cloud")[0].available is False


def test_price_monitor_reports_price_change(tmp_path: Path) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    offers_for(available=True, price="5.99"),
                    offers_for(available=True, price="4.99"),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.restocked is False
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.source_metrics[0].value == "€4.99/mo"
        assert message.entry.source_metrics[1].value == "€5.99/mo"


def test_discovery_summary_is_stable_and_silent() -> None:
    strategy = HetznerCloudStrategy()

    entries, title = strategy.fetch_entries(FEED_URL)

    assert title == "Hetzner Cloud"
    assert len(entries) == 1
    assert strategy.get_entry_id(entries[0]) == "hetzner-cloud:summary"
    data = strategy.get_entry_data(entries[0])
    assert data.title == "Hetzner Cloud availability"
    assert data.link == "https://console.hetzner.cloud/"


def test_config_accepts_hetzner_cloud_strategy() -> None:
    feed = FeedConfig(
        id="hetzner-cloud",
        url=FEED_URL,
        webhook="https://discord.test/webhook",
        strategy="hetzner_cloud",
        price_check_interval=120,
        restock_alerts=True,
    )

    assert feed.strategy == "hetzner_cloud"
    assert feed.price_check_interval == 120
