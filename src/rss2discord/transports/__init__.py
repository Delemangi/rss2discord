"""Scraping strategies for different sources."""

from .base import FeedFetchError, ScraperStrategy
from .gjirafa50 import Gjirafa50Strategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .pazar3 import Pazar3Strategy
from .rss import RSSStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "FeedFetchError",
    "Gjirafa50Strategy",
    "ITMkOglasnikStrategy",
    "Pazar3Strategy",
    "RSSStrategy",
    "ScraperStrategy",
    "XenForoStrategy",
]
