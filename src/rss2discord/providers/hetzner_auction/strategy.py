"""Hetzner server-auction discovery strategy."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.hetzner_auction.bounds import HETZNER_AUCTION_LABEL
from rss2discord.providers.hetzner_auction.catalog import (
    HetznerAuctionCatalogClient,
)
from rss2discord.providers.hetzner_auction.models import HetznerAuctionServer
from rss2discord.transports.base import ScraperStrategy


@final
class HetznerAuctionStrategy(ScraperStrategy):
    """Discover matching Hetzner auction servers."""

    seed_existing_on_first_fetch = True

    def __init__(self, client: HetznerAuctionCatalogClient | None = None) -> None:
        self._client = client or HetznerAuctionCatalogClient()

    @override
    def fetch_entries(self, url: str) -> tuple[list[HetznerAuctionServer], str]:
        return list(self._client.fetch_servers(url)), HETZNER_AUCTION_LABEL

    @override
    def get_entry_id(self, entry: HetznerAuctionServer) -> EntryId:
        return EntryId(f"{entry.id}:{_price_key(entry.prices.monthly.eur)}")

    @override
    def get_entry_data(self, entry: HetznerAuctionServer) -> EntryData:
        hardware = entry.hardware
        price = entry.prices.monthly.eur
        price_text = _price_key(price)
        ram = f"{hardware.ram.size} GB"
        if hardware.ram.ecc:
            ram += " ECC"
        storage = _storage_summary(entry)
        description_lines = [f"Server ID: {entry.id}"]
        next_cut = _next_cut(entry)
        if next_cut is not None:
            description_lines.append(f"Next price reduction: {next_cut}")
        return EntryData(
            title=(f"{hardware.cpu.name} · {ram} · €{price_text}/mo"),
            link=f"https://www.hetzner.com/sb/?freetext={entry.id}",
            description="\n".join(description_lines),
            author="Hetzner",
            timestamp=datetime.now(UTC).isoformat(),
            categories=("server-auction",),
            source_metrics=(
                SourceMetric(label="CPU", value=hardware.cpu.name),
                SourceMetric(label="RAM", value=ram),
                SourceMetric(label="Storage", value=storage),
                SourceMetric(
                    label="Location",
                    value=entry.details.datacenter.name,
                ),
                SourceMetric(label="Price", value=f"€{price_text}/month"),
            ),
        )


def _price_key(price: Decimal) -> str:
    return format(price.normalize(), "f")


def _storage_summary(entry: HetznerAuctionServer) -> str:
    storage = entry.hardware.storage
    if storage.disks:
        return "; ".join(storage.disks)
    return f"{storage.amount}× {storage.size} GB"


def _next_cut(entry: HetznerAuctionServer) -> str | None:
    timestamp = entry.timer.reduce_next_timestamp
    if timestamp is None:
        return None
    try:
        moment = datetime.fromtimestamp(timestamp, UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d %H:%M UTC")
