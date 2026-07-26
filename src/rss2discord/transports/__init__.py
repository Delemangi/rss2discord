"""Scraping strategies for different sources."""

from .anhoch import AnhochStrategy
from .base import FeedFetchError, ScraperStrategy
from .itmk_oglasnik import ITMkOglasnikStrategy
from .neksio import NeksioStrategy
from .rss import RSSStrategy
from .setec import SetecStrategy
from .xenforo import XenForoStrategy

__all__ = [
    "AnhochStrategy",
    "FeedFetchError",
    "ITMkOglasnikStrategy",
    "NeksioStrategy",
    "RSSStrategy",
    "ScraperStrategy",
    "SetecStrategy",
    "XenForoStrategy",
]
