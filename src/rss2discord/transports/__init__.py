"""Scraping strategies for different sources."""

from .anhoch import AnhochStrategy
from .base import FeedFetchError, ScraperStrategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .rss import RSSStrategy
from .setec import SetecStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "AnhochStrategy",
    "FeedFetchError",
    "ITMkOglasnikStrategy",
    "RSSStrategy",
    "ScraperStrategy",
    "SetecStrategy",
    "XenForoStrategy",
]
