"""Canonical provider strategy metadata and display labels."""

from typing import Final

PRICE_STRATEGIES: Final = frozenset(
    {
        "ananas",
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
        "woocommerce",
    },
)
STRATEGY_LABELS: Final = {
    "ananas": "Ananas",
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
    "woocommerce": "WooCommerce",
    "xenforo": "Forum",
}
ADAPTER_LABELS: Final = {
    "hackernews": "Hacker News",
    "reddit": "Reddit",
}
