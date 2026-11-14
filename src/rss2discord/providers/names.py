"""Canonical provider strategy names and display labels."""

from typing import Final

STRATEGY_NAMES: Final = (
    "anhoch",
    "cccenter",
    "ddstore",
    "gjirafa50",
    "hivetec",
    "itmk_oglasnik",
    "neksio",
    "neptun",
    "pazar3",
    "reklama5",
    "rss",
    "setec",
    "technomarket",
    "xenforo",
)
PRICE_STRATEGIES: Final = frozenset(
    {
        "anhoch",
        "cccenter",
        "ddstore",
        "gjirafa50",
        "hivetec",
        "neksio",
        "neptun",
        "pazar3",
        "reklama5",
        "setec",
        "technomarket",
    },
)
STRATEGY_LABELS: Final = {
    "anhoch": "Anhoch",
    "cccenter": "CCCenter",
    "ddstore": "DDStore",
    "gjirafa50": "Gjirafa50",
    "hivetec": "Hivetec",
    "itmk_oglasnik": "IT.mk Oglasnik",
    "neksio": "Neksio",
    "neptun": "Neptun",
    "pazar3": "Pazar3",
    "reklama5": "Reklama5",
    "rss": "RSS",
    "setec": "Setec",
    "technomarket": "Technomarket",
    "xenforo": "Forum",
}
ADAPTER_LABELS: Final = {
    "hackernews": "Hacker News",
    "reddit": "Reddit",
}
