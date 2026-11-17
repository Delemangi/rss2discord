from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Final

from rss2discord.providers.reklama5.scope import (
    REKLAMA5_LABEL,
    Reklama5PageRequest,
)
from rss2discord.providers.reklama5.scope import (
    Reklama5SearchScope as _Reklama5SearchScope,
)
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.listing_http import ListingHttpConfig, fetch_listing_page

Reklama5SearchScope = _Reklama5SearchScope

REKLAMA5_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
MAX_REKLAMA5_RESPONSE_BYTES: Final = 2_097_152
MAX_REKLAMA5_ATTEMPT_BYTES: Final = 6_291_456
MAX_REKLAMA5_REDIRECTS: Final = 10
MAX_REKLAMA5_ATTEMPT_SECONDS: Final = 120.0
MAX_REKLAMA5_CATALOG_ATTEMPT_BYTES: Final = 500 * 1024 * 1024
MAX_REKLAMA5_CATALOG_ATTEMPT_SECONDS: Final = 600.0
MAX_REKLAMA5_RATE_LIMIT_RETRIES: Final = 3
REKLAMA5_DEFAULT_RETRY_AFTER_SECONDS: Final = 10.0

REKLAMA5_BROWSER_HEADERS: Final = (
    ("Accept-Language", "mk-MK,mk;q=0.9,en;q=0.8"),
    ("Upgrade-Insecure-Requests", "1"),
    ("Sec-Fetch-Dest", "document"),
    ("Sec-Fetch-Mode", "navigate"),
    ("Sec-Fetch-Site", "none"),
    ("Sec-Fetch-User", "?1"),
    ("Referer", "https://reklama5.mk/"),
)

REKLAMA5_LISTING_CONFIG: Final = ListingHttpConfig(
    label=REKLAMA5_LABEL,
    max_response_bytes=MAX_REKLAMA5_RESPONSE_BYTES,
    user_agent=REKLAMA5_USER_AGENT,
    challenge_cause="BotChallenge",
    extra_headers=REKLAMA5_BROWSER_HEADERS,
)


@dataclass(slots=True)  # noqa: RUF100  # noqa: MUTABLE_OK
class Reklama5ScanBudget:
    """Track mutable byte, redirect, and deadline limits for one fetch attempt."""

    bytes_remaining: int
    redirects_remaining: int
    expires_at: float

    @classmethod
    def for_attempt(cls) -> Reklama5ScanBudget:
        return cls(
            bytes_remaining=MAX_REKLAMA5_ATTEMPT_BYTES,
            redirects_remaining=MAX_REKLAMA5_REDIRECTS,
            expires_at=time.monotonic() + MAX_REKLAMA5_ATTEMPT_SECONDS,
        )

    @classmethod
    def for_catalog(cls) -> Reklama5ScanBudget:
        return cls(
            bytes_remaining=MAX_REKLAMA5_CATALOG_ATTEMPT_BYTES,
            redirects_remaining=MAX_REKLAMA5_REDIRECTS,
            expires_at=time.monotonic() + MAX_REKLAMA5_CATALOG_ATTEMPT_SECONDS,
        )

    def request_timeout(self) -> float:
        remaining_seconds = self.expires_at - time.monotonic()
        if remaining_seconds <= 0:
            raise FeedFetchError(REKLAMA5_LABEL, "ScanTimeLimitExceeded")
        return min(30.0, remaining_seconds)

    def consume_redirect(self) -> None:
        if self.redirects_remaining <= 0:
            raise FeedFetchError(REKLAMA5_LABEL, "TooManyRedirects")
        self.redirects_remaining -= 1

    def consume_bytes(self, size: int) -> None:
        if time.monotonic() >= self.expires_at:
            raise FeedFetchError(REKLAMA5_LABEL, "ScanTimeLimitExceeded")
        if size > self.bytes_remaining:
            raise FeedFetchError(REKLAMA5_LABEL, "ScanByteLimitExceeded")
        self.bytes_remaining -= size


def fetch_reklama5_page(
    request: Reklama5PageRequest,
    budget: Reklama5ScanBudget,
) -> bytes:
    """Fetch one trusted search page within a caller-owned attempt budget."""
    for attempt in range(1, MAX_REKLAMA5_RATE_LIMIT_RETRIES + 1):
        try:
            return fetch_listing_page(
                url=request.url,
                budget=budget,
                config=REKLAMA5_LISTING_CONFIG,
                accepts_redirect=lambda target: request.scope.accepts_redirect(
                    request,
                    target,
                ),
            )
        except FeedFetchError as error:
            if (
                error.cause_type != "HTTPError"
                or error.status_code != 429
                or attempt == MAX_REKLAMA5_RATE_LIMIT_RETRIES
            ):
                raise
            delay = error.retry_after or REKLAMA5_DEFAULT_RETRY_AFTER_SECONDS
            if time.monotonic() + delay >= budget.expires_at:
                raise
            time.sleep(delay)
    raise AssertionError
