"""Source-specific entry adapters."""

from .base import AdapterError, SourceAdapter
from .hackernews import HackerNewsAdapter
from .reddit import RedditAdapter

__all__ = ["AdapterError", "HackerNewsAdapter", "RedditAdapter", "SourceAdapter"]
