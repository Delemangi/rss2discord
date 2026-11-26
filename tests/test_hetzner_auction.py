"""Hetzner auction provider behavior."""

import json
from decimal import Decimal

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.hetzner_auction.catalog import (
    HetznerAuctionCatalogClient,
)
from rss2discord.providers.hetzner_auction.models import (
    HetznerAuctionFilters,
    HetznerAuctionServer,
)
from rss2discord.providers.hetzner_auction.strategy import (
    HetznerAuctionStrategy,
)

BASE_URL = "https://www.hetzner.com/_resources/app/data/app/live_data_sb.json"
FILTERED_URL = (
    BASE_URL
    + "?max_price_eur=150&min_ram_gb=64&require_ecc=true"
    + "&disk_type=nvme&min_disk_gb=512&location=FSN1&cpu_search=i7"
)


def raw_server(
    *,
    server_id: int = 1,
    cpu: str = "Intel Core i7-6700",
    ram_gb: int = 64,
    ecc: bool = False,
    nvme: tuple[int, ...] = (512, 512),
    sata: tuple[int, ...] = (),
    hdd: tuple[int, ...] = (),
    price: int | float = 79,
    fixed: bool = False,
    location: str = "FSN1-DC8",
    reduce_next_timestamp: int | None = 1_795_414_137,
) -> dict[str, object]:
    disks = [f"{size} GB NVMe" for size in nvme]
    disks.extend(f"{size} GB SATA" for size in sata)
    disks.extend(f"{size} GB HDD" for size in hdd)
    return {
        "Id": server_id,
        "Hardware": {
            "CPU": {"Name": cpu},
            "RAM": {"Size": ram_gb, "ecc": ecc},
            "Storage": {
                "Size": nvme[0] if nvme else 0,
                "Amount": len(disks),
                "Disks": disks,
                "Details": {
                    "nvme": list(nvme),
                    "sata": list(sata),
                    "hdd": list(hdd),
                },
            },
        },
        "Prices": {
            "monthly": {"EUR": price},
            "fixed": fixed,
        },
        "Details": {"Datacenter": {"Name": location}},
        "Timer": {"ReduceNextTimestamp": reduce_next_timestamp},
    }


def client_with(*servers: dict[str, object]) -> HetznerAuctionCatalogClient:
    payload = json.dumps({"server": list(servers)}).encode()
    return HetznerAuctionCatalogClient(fetch=lambda _: payload)


def test_filters_parse_url_query() -> None:
    filters = HetznerAuctionFilters.from_url(FILTERED_URL)

    assert filters.max_price_eur == Decimal(150)
    assert filters.min_ram_gb == 64
    assert filters.require_ecc is True
    assert filters.disk_type == "nvme"
    assert filters.min_disk_gb == 512
    assert filters.location == "FSN1"
    assert filters.cpu_search == "i7"
    assert filters.include_fixed is False


@pytest.mark.parametrize(
    "url",
    [
        "http://www.hetzner.com/_resources/app/data/app/live_data_sb.json",
        "https://example.com/_resources/app/data/app/live_data_sb.json",
        "https://www.hetzner.com/other.json",
        BASE_URL + "?unknown=1",
        BASE_URL + "?disk_type=optane",
        BASE_URL + "?max_price_eur=-1",
        BASE_URL + "?require_ecc=maybe",
    ],
)
def test_filters_reject_invalid_urls(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        HetznerAuctionFilters.from_url(url)

    assert error.value.cause_type == "InvalidUrl"


def test_catalog_returns_only_matching_servers() -> None:
    client = client_with(
        raw_server(server_id=1, ecc=True),
        raw_server(server_id=2, ram_gb=32),
        raw_server(server_id=3, price=400),
        raw_server(server_id=4, nvme=(), sata=(512, 512)),
        raw_server(server_id=5, location="HEL1-DC2"),
        raw_server(server_id=6, cpu="AMD Ryzen 5 3600"),
    )

    servers = client.fetch_servers(FILTERED_URL)

    assert [server.id for server in servers] == [1]


def test_catalog_excludes_fixed_listings_by_default() -> None:
    client = client_with(
        raw_server(server_id=1, fixed=True),
        raw_server(server_id=2),
    )

    assert [server.id for server in client.fetch_servers(BASE_URL)] == [2]


def test_catalog_can_include_fixed_listings() -> None:
    client = client_with(
        raw_server(server_id=1, fixed=True),
        raw_server(server_id=2),
    )

    servers = client.fetch_servers(BASE_URL + "?include_fixed=true")

    assert [server.id for server in servers] == [1, 2]


def test_catalog_skips_invalid_servers() -> None:
    client = client_with(
        raw_server(server_id=1),
        {"Id": 2, "Prices": {}},
    )

    servers = client.fetch_servers(BASE_URL)

    assert [server.id for server in servers] == [1]


@pytest.mark.parametrize(
    ("content", "cause"),
    [
        (b"not json", "InvalidResponse"),
        (json.dumps({"server": []}).encode(), "EmptyResponse"),
        (json.dumps({"server": [{"Id": 1}]}).encode(), "InvalidResponse"),
        (json.dumps({"servers": []}).encode(), "InvalidResponse"),
    ],
)
def test_catalog_rejects_invalid_responses(content: bytes, cause: str) -> None:
    client = HetznerAuctionCatalogClient(fetch=lambda _: content)

    with pytest.raises(FeedFetchError) as error:
        client.fetch_servers(BASE_URL)

    assert error.value.cause_type == cause


def test_strategy_entry_id_changes_when_price_crosses_a_bucket() -> None:
    strategy = HetznerAuctionStrategy(client=client_with())
    cheap = HetznerAuctionServer.model_validate(raw_server(price=79))
    pricier = HetznerAuctionServer.model_validate(raw_server(price=80))
    five_euros_cheaper = HetznerAuctionServer.model_validate(raw_server(price=75))

    assert strategy.get_entry_id(cheap) == "1:75"
    assert strategy.get_entry_id(pricier) == "1:80"
    assert strategy.get_entry_id(five_euros_cheaper) == "1:75"
    assert strategy.get_entry_id(pricier) != strategy.get_entry_id(five_euros_cheaper)


def test_strategy_entry_id_ignores_one_euro_price_cuts() -> None:
    strategy = HetznerAuctionStrategy(client=client_with())
    first = HetznerAuctionServer.model_validate(raw_server(price=79))
    one_euro_lower = HetznerAuctionServer.model_validate(raw_server(price=78))
    expensive = HetznerAuctionServer.model_validate(raw_server(price=518))
    expensive_lower = HetznerAuctionServer.model_validate(raw_server(price=517))

    assert strategy.get_entry_id(first) == strategy.get_entry_id(one_euro_lower)
    assert strategy.get_entry_id(expensive) == strategy.get_entry_id(expensive_lower)


def test_strategy_builds_card_fields() -> None:
    strategy = HetznerAuctionStrategy(client=client_with())
    server = HetznerAuctionServer.model_validate(raw_server(ecc=True))

    data = strategy.get_entry_data(server)

    assert "Intel Core i7-6700" in data.title
    assert "€79/mo" in data.title
    assert data.link == "https://www.hetzner.com/sb/?freetext=1"
    assert data.author == "Hetzner"
    assert data.timestamp is not None
    assert "Server ID: 1" in data.description
    assert "Next price reduction:" in data.description
    assert [metric.label for metric in data.source_metrics] == [
        "CPU",
        "RAM",
        "Storage",
        "Location",
        "Price",
    ]
    assert data.source_metrics[1].value == "64 GB ECC"
    assert data.source_metrics[2].value == "512 GB NVMe; 512 GB NVMe"


def test_config_accepts_hetzner_auction_strategy() -> None:
    feed = FeedConfig(
        id="hetzner-auction",
        url=FILTERED_URL,
        webhook="https://discord.test/webhook",
        strategy="hetzner_auction",
    )

    assert feed.strategy == "hetzner_auction"
