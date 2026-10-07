"""Scraping strategies for different sources."""

from .base import FeedFetchError, ScraperStrategy
from .gjirafa50 import Gjirafa50Strategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .reklama5 import Reklama5Strategy
from .rss import RSSStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "FeedFetchError",
    "Gjirafa50Strategy",
    "ITMkOglasnikStrategy",
    "RSSStrategy",
    "Reklama5Strategy",
    "ScraperStrategy",
    "XenForoStrategy",
]
