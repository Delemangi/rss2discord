"""Canonical provider strategy metadata and display labels."""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final


@dataclass(frozen=True, slots=True)
class StrategyMetadata:
    """Display and capability metadata for one feed strategy."""

    label: str
    price: bool = False


STRATEGY_METADATA: Final[Mapping[str, StrategyMetadata]] = MappingProxyType(
    {
        "ananas": StrategyMetadata("Ananas", price=True),
        "anhoch": StrategyMetadata("Anhoch", price=True),
        "cccenter": StrategyMetadata("CCCenter", price=True),
        "ddstore": StrategyMetadata("DDStore", price=True),
        "emsc": StrategyMetadata("EMSC"),
        "fakulteti_mk": StrategyMetadata("Fakulteti.mk"),
        "gjirafa50": StrategyMetadata("Gjirafa50", price=True),
        "hivetec": StrategyMetadata("Hivetec", price=True),
        "itmk_oglasnik": StrategyMetadata("IT.mk Oglasnik"),
        "neksio": StrategyMetadata("Neksio", price=True),
        "neptun": StrategyMetadata("Neptun", price=True),
        "pazar3": StrategyMetadata("Pazar3", price=True),
        "proxnet": StrategyMetadata("PROXNet", price=True),
        "reklama5": StrategyMetadata("Reklama5", price=True),
        "reddit": StrategyMetadata("Reddit"),
        "rss": StrategyMetadata("RSS"),
        "setec": StrategyMetadata("Setec", price=True),
        "technomarket": StrategyMetadata("Technomarket", price=True),
        "time_mk": StrategyMetadata("Time.mk"),
        "tweets": StrategyMetadata("Tweets"),
        "woocommerce": StrategyMetadata("WooCommerce", price=True),
        "xenforo": StrategyMetadata("Forum"),
    },
)
STRATEGY_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {name: metadata.label for name, metadata in STRATEGY_METADATA.items()},
)
PRICE_STRATEGIES: Final[frozenset[str]] = frozenset(
    name for name, metadata in STRATEGY_METADATA.items() if metadata.price
)
ADAPTER_LABELS: Final[dict[str, str]] = {
    "hackernews": "Hacker News",
    "reddit": "Reddit",
}
