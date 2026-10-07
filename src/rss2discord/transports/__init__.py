"""Scraping strategies for different sources."""

from .base import FeedFetchError, ScraperStrategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .pazar3 import Pazar3Strategy
from .reklama5 import Reklama5Strategy
from .rss import RSSStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "FeedFetchError",
    "ITMkOglasnikStrategy",
    "Pazar3Strategy",
    "RSSStrategy",
    "Reklama5Strategy",
    "ScraperStrategy",
    "XenForoStrategy",
]
