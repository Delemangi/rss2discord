"""Hetzner Cloud server-type models and availability offers."""

from dataclasses import dataclass
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _HetznerCloudModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class HetznerCloudDeprecation(_HetznerCloudModel):
    announced: str | None = None
    unavailable_after: str | None = None


class HetznerCloudLocation(_HetznerCloudModel):
    id: int = 0
    name: str = ""
    available: bool = False
    recommended: bool = False
    deprecation: HetznerCloudDeprecation | None = None


class HetznerCloudMoney(_HetznerCloudModel):
    gross: Decimal
    net: Decimal | None = None


class HetznerCloudPrice(_HetznerCloudModel):
    location: str = ""
    price_monthly: HetznerCloudMoney
    price_hourly: HetznerCloudMoney | None = None


class HetznerCloudServerType(_HetznerCloudModel):
    id: int = 0
    name: str = ""
    description: str = ""
    cores: int = 0
    memory: Decimal = Decimal(0)
    disk: int = 0
    architecture: str = ""
    cpu_type: str = ""
    storage_type: str = ""
    deprecation: HetznerCloudDeprecation | None = None
    locations: tuple[HetznerCloudLocation, ...] = ()
    prices: tuple[HetznerCloudPrice, ...] = ()


class HetznerCloudServerTypesResponse(_HetznerCloudModel):
    server_types: tuple[HetznerCloudServerType, ...]


@dataclass(frozen=True, slots=True)
class HetznerCloudFilters:
    """Optional feed-level filters applied before offers become snapshots."""

    server_types: tuple[str, ...] | None = None
    locations: tuple[str, ...] | None = None


@dataclass(frozen=True, slots=True)
class HetznerCloudOffer:
    """One server type in one location, with its monthly price."""

    server_type: HetznerCloudServerType
    location: HetznerCloudLocation
    price: Decimal
