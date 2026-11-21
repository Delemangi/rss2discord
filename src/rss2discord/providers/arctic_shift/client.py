"""Bounded HTTP client for the Arctic Shift Reddit archive."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any, Final, Protocol

import requests
from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.arctic_shift.bounds import (
    ARCTIC_SHIFT_LABEL,
    ARCTIC_SHIFT_ORIGIN,
    ARCTIC_SHIFT_PAGE_SIZE,
    ARCTIC_SHIFT_RATE_LIMIT_BACKOFF_SECONDS,
    ARCTIC_SHIFT_REQUEST_SECONDS,
    ARCTIC_SHIFT_SEARCH_PATH,
    ARCTIC_SHIFT_STREAM_CHUNK_BYTES,
    ARCTIC_SHIFT_USER_AGENT,
    MAX_ARCTIC_SHIFT_RESPONSE_BYTES,
)
from rss2discord.providers.reddit.models import RedditPage, RedditPost
from rss2discord.providers.reddit.scope import RedditScope
from rss2discord.retries import FeedFetchInterruptedError, parse_retry_after
from rss2discord.transports.bounded_http import read_bounded_content

_RETRYABLE_REQUEST_ERRORS: Final = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)
_MAX_RATE_LIMIT_DELTA_SECONDS: Final = 3_600.0


class ArcticShiftSession(Protocol):
    """Minimal HTTP session surface used by the Arctic Shift client."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> Any: ...  # noqa: ANN401


class ArcticShiftClient:
    """Read newest-first subreddit posts from the public Arctic Shift archive."""

    def __init__(
        self,
        *,
        session: ArcticShiftSession | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
        timeout: float = ARCTIC_SHIFT_REQUEST_SECONDS,
        max_response_bytes: int = MAX_ARCTIC_SHIFT_RESPONSE_BYTES,
        page_size: int = ARCTIC_SHIFT_PAGE_SIZE,
        user_agent: str = ARCTIC_SHIFT_USER_AGENT,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        if page_size > ARCTIC_SHIFT_PAGE_SIZE:
            raise ValueError(
                f"page_size must not exceed {ARCTIC_SHIFT_PAGE_SIZE}",
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        self._session = session
        self._is_shutdown_requested = is_shutdown_requested
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._page_size = page_size
        self._user_agent = user_agent

    def fetch_listing(
        self,
        scope: RedditScope,
        *,
        after: str | None = None,
        count: int = 0,
    ) -> RedditPage:
        """Fetch one newest-first page, paginating older with ``before``."""
        del count
        if scope.sort != "new":
            raise FeedFetchError(ARCTIC_SHIFT_LABEL, "UnsupportedSort")
        if self._is_shutdown_requested():
            raise FeedFetchInterruptedError
        params: dict[str, str | int] = {
            "subreddit": scope.subreddit,
            "limit": self._page_size,
            "sort": "desc",
        }
        if after is not None:
            params["before"] = after
        return self._parse_listing(self._get(params))

    def _get(self, params: Mapping[str, str | int]) -> bytes:
        url = f"{ARCTIC_SHIFT_ORIGIN}{ARCTIC_SHIFT_SEARCH_PATH}"
        headers = {"User-Agent": self._user_agent}
        try:
            if self._session is not None:
                response = self._session.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                    stream=True,
                )
            else:
                response = requests.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout=self._timeout,
                    stream=True,
                )
            with response:
                if response.status_code >= 400:
                    raise _http_error(response.status_code, response.headers)
                return read_bounded_content(
                    response,
                    label=ARCTIC_SHIFT_LABEL,
                    max_bytes=self._max_response_bytes,
                    chunk_bytes=ARCTIC_SHIFT_STREAM_CHUNK_BYTES,
                )
        except FeedFetchError:
            raise
        except _RETRYABLE_REQUEST_ERRORS as error:
            raise FeedFetchError(
                ARCTIC_SHIFT_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(ARCTIC_SHIFT_LABEL, type(error).__name__) from None

    def _parse_listing(self, content: bytes) -> RedditPage:
        try:
            payload = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise FeedFetchError(ARCTIC_SHIFT_LABEL, "InvalidJson") from None
        if not isinstance(payload, dict):
            raise FeedFetchError(ARCTIC_SHIFT_LABEL, "InvalidJson")
        raw_posts = payload.get("data")
        if not isinstance(raw_posts, list):
            raise FeedFetchError(ARCTIC_SHIFT_LABEL, "InvalidJson")

        posts: list[RedditPost] = []
        for item in raw_posts:
            if not isinstance(item, dict):
                continue
            try:
                posts.append(RedditPost.model_validate(item))
            except ValidationError:
                continue

        return RedditPage(
            posts=tuple(posts),
            after=self._next_cursor(raw_posts),
        )

    def _next_cursor(self, raw_posts: list[Any]) -> str | None:
        # Only a full page can have an older page behind it.
        if len(raw_posts) != self._page_size:
            return None
        for item in reversed(raw_posts):
            if not isinstance(item, dict):
                continue
            created_utc = item.get("created_utc")
            if isinstance(created_utc, bool):
                continue
            if isinstance(created_utc, int | float):
                # before= is exclusive. Advance one second so posts sharing
                # the boundary second are fetched; the strategy dedupes them.
                return str(int(created_utc) + 1)
            if isinstance(created_utc, str) and created_utc.isdigit():
                return str(int(created_utc) + 1)
        return None


def _http_error(status_code: int, headers: Mapping[str, str]) -> FeedFetchError:
    if status_code == 429:
        retry_after = _retry_after(headers)
    elif status_code == 422:
        # Arctic Shift uses 422 for "Timeout. Maybe slow down a bit".
        retry_after = ARCTIC_SHIFT_RATE_LIMIT_BACKOFF_SECONDS
    else:
        retry_after = None
    return FeedFetchError(
        ARCTIC_SHIFT_LABEL,
        "HTTPError",
        status_code=status_code,
        retryable=status_code in {422, 429} or 500 <= status_code < 600,
        retry_after=retry_after,
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    retry_after = parse_retry_after(headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after
    reset = parse_retry_after(headers.get("X-RateLimit-Reset"))
    # A delta only; an epoch timestamp would otherwise sleep for decades.
    if reset is not None and 0 < reset <= _MAX_RATE_LIMIT_DELTA_SECONDS:
        return reset
    return None
