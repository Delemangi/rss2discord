"""Scraping strategies for different sources."""

from .base import FeedFetchError, ScraperStrategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .rss import RSSStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "FeedFetchError",
    "ITMkOglasnikStrategy",
    "RSSStrategy",
    "ScraperStrategy",
    "XenForoStrategy",
]
