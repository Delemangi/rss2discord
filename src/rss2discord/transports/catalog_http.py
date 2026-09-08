"""Shared bounded HTTP response callback behavior for catalog transports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import FeedFetchInterruptedError


class ContentBudget(Protocol):
    def before_chunk(self) -> None: ...

    def add_bytes(self, amount: int) -> None: ...


@dataclass(slots=True)
class BoundedContentCallback:
    """Collect one response while preserving callback abort causes."""

    budget: ContentBudget | None
    max_bytes: int
    label: str
    content: bytearray
    abort_error: FeedFetchError | FeedFetchInterruptedError | None = None

    @classmethod
    def start(
        cls,
        budget: ContentBudget | None,
        *,
        max_bytes: int,
        label: str,
    ) -> BoundedContentCallback:
        return cls(budget, max_bytes, label, bytearray())

    def write(self, chunk: bytes) -> int:
        if self.abort_error is not None:
            return CURL_WRITEFUNC_ERROR
        try:
            if self.budget is not None:
                self.budget.before_chunk()
            if len(self.content) + len(chunk) > self.max_bytes:
                self.abort_error = FeedFetchError(self.label, "ResponseTooLarge")
                return CURL_WRITEFUNC_ERROR
            self.content.extend(chunk)
            if self.budget is not None:
                self.budget.add_bytes(len(chunk))
        except (FeedFetchError, FeedFetchInterruptedError) as error:
            self.abort_error = error
            return CURL_WRITEFUNC_ERROR
        return len(chunk)
