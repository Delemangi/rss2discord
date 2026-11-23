"""Hetzner auction feed models, filters, and URL normalization."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.hetzner_auction.bounds import (
    HETZNER_AUCTION_HOST,
    HETZNER_AUCTION_LABEL,
    HETZNER_AUCTION_PATH,
)

DiskType = Literal["nvme", "sata", "hdd"]

_ALLOWED_QUERY_KEYS = frozenset(
    {
        "cpu_search",
        "disk_type",
        "include_fixed",
        "location",
        "max_price_eur",
        "min_disk_gb",
        "min_ram_gb",
        "require_ecc",
    },
)


class _HetznerModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class HetznerCpu(_HetznerModel):
    name: str = Field(alias="Name")


class HetznerRam(_HetznerModel):
    size: int = Field(alias="Size")
    ecc: bool = Field(default=False, alias="ecc")


class HetznerStorageDetails(_HetznerModel):
    nvme: tuple[int, ...] = Field(default=(), alias="nvme")
    sata: tuple[int, ...] = Field(default=(), alias="sata")
    hdd: tuple[int, ...] = Field(default=(), alias="hdd")


class HetznerStorage(_HetznerModel):
    size: int = Field(alias="Size")
    amount: int = Field(alias="Amount")
    disks: tuple[str, ...] = Field(default=(), alias="Disks")
    details: HetznerStorageDetails = Field(
        default_factory=HetznerStorageDetails,
        alias="Details",
    )


class HetznerHardware(_HetznerModel):
    cpu: HetznerCpu = Field(alias="CPU")
    ram: HetznerRam = Field(alias="RAM")
    storage: HetznerStorage = Field(alias="Storage")


class HetznerMonthlyPrice(_HetznerModel):
    eur: Decimal = Field(alias="EUR")


class HetznerPrices(_HetznerModel):
    monthly: HetznerMonthlyPrice = Field(alias="monthly")
    fixed: bool = Field(default=False, alias="fixed")


class HetznerDatacenter(_HetznerModel):
    name: str = Field(alias="Name")


class HetznerDetails(_HetznerModel):
    datacenter: HetznerDatacenter = Field(alias="Datacenter")


class HetznerTimer(_HetznerModel):
    reduce_next_timestamp: int | None = Field(
        default=None,
        alias="ReduceNextTimestamp",
    )


class HetznerAuctionServer(_HetznerModel):
    id: int = Field(alias="Id")
    hardware: HetznerHardware = Field(alias="Hardware")
    prices: HetznerPrices = Field(alias="Prices")
    details: HetznerDetails = Field(alias="Details")
    timer: HetznerTimer = Field(default_factory=HetznerTimer, alias="Timer")


@dataclass(frozen=True, slots=True)
class HetznerAuctionFilters:
    """Optional query filters applied before listings become entries."""

    max_price_eur: Decimal | None = None
    min_ram_gb: int | None = None
    require_ecc: bool = False
    disk_type: DiskType | None = None
    min_disk_gb: int | None = None
    location: str | None = None
    cpu_search: str | None = None
    include_fixed: bool = False

    @classmethod
    def from_url(cls, url: str) -> "HetznerAuctionFilters":
        """Validate a feed URL and decode its optional filter query."""
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != HETZNER_AUCTION_HOST
            or port not in {None, 443}
            or parsed.path != HETZNER_AUCTION_PATH
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if set(query) - _ALLOWED_QUERY_KEYS:
            raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")

        def single(key: str) -> str | None:
            values = query.get(key)
            if not values:
                return None
            if len(values) != 1:
                raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")
            value = values[0].strip()
            if not value:
                raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")
            return value

        max_price = single("max_price_eur")
        min_ram = single("min_ram_gb")
        disk_type = single("disk_type")
        min_disk = single("min_disk_gb")
        location = single("location")
        cpu_search = single("cpu_search")
        return cls(
            max_price_eur=_decimal(max_price) if max_price is not None else None,
            min_ram_gb=_positive_int(min_ram, minimum=1),
            require_ecc=_boolean(single("require_ecc")),
            disk_type=_disk_type(disk_type),
            min_disk_gb=_positive_int(min_disk, minimum=0),
            location=_bounded_text(location, 64),
            cpu_search=_bounded_text(cpu_search, 128),
            include_fixed=_boolean(single("include_fixed")),
        )

    def matches(self, server: HetznerAuctionServer) -> bool:
        """Return whether one listing satisfies every configured filter."""
        if not self.include_fixed and server.prices.fixed:
            return False
        if not self._matches_price(server.prices.monthly.eur):
            return False
        hardware = server.hardware
        if not self._matches_ram(hardware.ram):
            return False
        if not self._matches_cpu(hardware.cpu.name):
            return False
        if not self._matches_location(server.details.datacenter.name):
            return False
        return self._matches_storage(hardware.storage.details)

    def _matches_price(self, price: Decimal) -> bool:
        return self.max_price_eur is None or price <= self.max_price_eur

    def _matches_ram(self, ram: HetznerRam) -> bool:
        if self.min_ram_gb is not None and ram.size < self.min_ram_gb:
            return False
        return not self.require_ecc or ram.ecc

    def _matches_cpu(self, cpu_name: str) -> bool:
        return self.cpu_search is None or (
            self.cpu_search.casefold() in cpu_name.casefold()
        )

    def _matches_location(self, datacenter_name: str) -> bool:
        return self.location is None or (
            self.location.casefold() in datacenter_name.casefold()
        )

    def _matches_storage(self, details: HetznerStorageDetails) -> bool:
        if self.disk_type is None and self.min_disk_gb is None:
            return True
        sizes = (
            getattr(details, self.disk_type)
            if self.disk_type is not None
            else (*details.nvme, *details.sata, *details.hdd)
        )
        if self.disk_type is not None and not sizes:
            return False
        return self.min_disk_gb is None or sum(sizes) >= self.min_disk_gb


def _decimal(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl") from None
    if not parsed.is_finite() or parsed < 0:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")
    return parsed


def _positive_int(value: str | None, *, minimum: int) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl") from None
    if parsed < minimum:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")
    return parsed


def _boolean(value: str | None) -> bool:
    if value is None:
        return False
    match value.casefold():
        case "1" | "true" | "yes" | "on":
            return True
        case "0" | "false" | "no" | "off":
            return False
        case _:
            raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")


def _disk_type(value: str | None) -> DiskType | None:
    if value is None:
        return None
    match value.casefold():
        case "nvme":
            return "nvme"
        case "sata":
            return "sata"
        case "hdd":
            return "hdd"
        case _:
            raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")


def _bounded_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    if len(value) > limit:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidUrl")
    return value
