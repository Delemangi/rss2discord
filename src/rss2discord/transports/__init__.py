"""Scraping strategies for different sources."""

from .anhoch import AnhochStrategy
from .base import FeedFetchError, ScraperStrategy
from .ddstore import DDStoreStrategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .neksio import NeksioStrategy
from .neptun import NeptunStrategy
from .pazar3 import Pazar3Strategy
from .reklama5 import Reklama5Strategy
from .rss import RSSStrategy
from .setec import SetecStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "AnhochStrategy",
    "DDStoreStrategy",
    "FeedFetchError",
    "ITMkOglasnikStrategy",
    "NeksioStrategy",
    "NeptunStrategy",
    "Pazar3Strategy",
    "RSSStrategy",
    "Reklama5Strategy",
    "ScraperStrategy",
    "SetecStrategy",
    "XenForoStrategy",
]
