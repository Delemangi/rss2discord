"""Scraping strategies for different sources."""

from .base import FeedFetchError, ScraperStrategy
from .cccenter import CCCenterStrategy
from .gjirafa50 import Gjirafa50Strategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .reklama5 import Reklama5Strategy
from .rss import RSSStrategy
from .technomarket import TechnomarketStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "CCCenterStrategy",
    "FeedFetchError",
    "Gjirafa50Strategy",
    "ITMkOglasnikStrategy",
    "RSSStrategy",
    "Reklama5Strategy",
    "ScraperStrategy",
    "TechnomarketStrategy",
    "XenForoStrategy",
]
