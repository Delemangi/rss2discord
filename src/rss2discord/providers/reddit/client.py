"""Bounded Reddit Data API transport with OAuth client-credentials auth."""

import json
import os
from collections.abc import Callable, Mapping
from threading import Lock
from time import monotonic
from time import sleep as default_sleep
from typing import Any, Final, Protocol

import requests
from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.reddit.bounds import (
    MAX_REDDIT_RESPONSE_BYTES,
    REDDIT_ACCESS_TOKEN_ENV,
    REDDIT_API_ORIGIN,
    REDDIT_CLIENT_ID_ENV,
    REDDIT_CLIENT_SECRET_ENV,
    REDDIT_DEFAULT_TOKEN_SECONDS,
    REDDIT_DEFAULT_USER_AGENT,
    REDDIT_LABEL,
    REDDIT_MIN_REQUEST_INTERVAL_SECONDS,
    REDDIT_PAGE_SIZE,
    REDDIT_REQUEST_SECONDS,
    REDDIT_STREAM_CHUNK_BYTES,
    REDDIT_TOKEN_SAFETY_SECONDS,
    REDDIT_TOKEN_URL,
    REDDIT_USER_AGENT_ENV,
)
from rss2discord.providers.reddit.models import (
    RedditChild,
    RedditListing,
    RedditPage,
    RedditPost,
    RedditToken,
)
from rss2discord.providers.reddit.scope import RedditScope
from rss2discord.retries import parse_retry_after
from rss2discord.transports.bounded_http import read_bounded_content

_RETRYABLE_REQUEST_ERRORS: Final = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


class RedditSession(Protocol):
    """Minimal HTTP session surface used by the Reddit client."""

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, str | int],
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> Any: ...  # noqa: ANN401

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        auth: tuple[str, str],
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> Any: ...  # noqa: ANN401


class RedditClient:
    """Acquire and cache an app-only token, then fetch bounded listing pages."""

    def __init__(
        self,
        *,
        session: RedditSession | None = None,
        clock: Callable[[], float] = monotonic,
        sleep: Callable[[float], None] = default_sleep,
        environment: Mapping[str, str] | None = None,
        min_request_interval: float = REDDIT_MIN_REQUEST_INTERVAL_SECONDS,
        timeout: float = REDDIT_REQUEST_SECONDS,
        max_response_bytes: int = MAX_REDDIT_RESPONSE_BYTES,
    ) -> None:
        self._session = session
        self._clock = clock
        self._sleep = sleep
        self._environment = environment
        self._min_request_interval = min_request_interval
        self._timeout = timeout
        self._max_response_bytes = max_response_bytes
        self._token_lock = Lock()
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._pace_lock = Lock()
        self._last_request_at: float | None = None

    def fetch_listing(
        self,
        scope: RedditScope,
        *,
        after: str | None = None,
        count: int = 0,
    ) -> RedditPage:
        """Fetch one newest-first listing page within the byte and time bounds."""
        url = f"{REDDIT_API_ORIGIN}/r/{scope.subreddit}/{scope.sort}"
        params: dict[str, str | int] = {
            "limit": REDDIT_PAGE_SIZE,
            "raw_json": 1,
        }
        if after is not None:
            params["after"] = after
        if count:
            params["count"] = count
        if scope.time_filter is not None:
            params["t"] = scope.time_filter
        return parse_reddit_listing(self._get_listing(url, params, allow_retry=True))

    def _get_listing(
        self,
        url: str,
        params: Mapping[str, str | int],
        *,
        allow_retry: bool,
    ) -> bytes:
        token = self._access_token()
        self._wait_for_request_slot()
        try:
            response = self._get(url, params, token)
            with response:
                if response.status_code == 401 and allow_retry:
                    self._invalidate_token(token)
                    return self._get_listing(url, params, allow_retry=False)
                if response.status_code >= 400:
                    raise _http_error(response.status_code, response.headers)
                return self._read_content(response)
        except FeedFetchError:
            raise
        except _RETRYABLE_REQUEST_ERRORS as error:
            raise FeedFetchError(
                REDDIT_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(REDDIT_LABEL, type(error).__name__) from None

    def _access_token(self) -> str:
        with self._token_lock:
            if self._token is not None and self._clock() < self._token_expires_at:
                return self._token
            token = self._request_token()
            self._token = token
            return token

    def _request_token(self) -> str:
        access_token = self._credential(REDDIT_ACCESS_TOKEN_ENV)
        if access_token:
            self._token_expires_at = float("inf")
            return access_token
        client_id = self._credential(REDDIT_CLIENT_ID_ENV)
        client_secret = self._credential(REDDIT_CLIENT_SECRET_ENV)
        if not client_id or not client_secret:
            raise FeedFetchError(REDDIT_LABEL, "MissingCredentials")
        user_agent = self._user_agent()
        self._wait_for_request_slot()
        try:
            response = self._post_token(client_id, client_secret, user_agent)
            with response:
                if response.status_code >= 400:
                    raise _http_error(response.status_code, response.headers)
                content = self._read_content(response)
        except FeedFetchError:
            raise
        except _RETRYABLE_REQUEST_ERRORS as error:
            raise FeedFetchError(
                REDDIT_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(REDDIT_LABEL, type(error).__name__) from None
        token = _parse_token(content)
        expires_in = token.expires_in or REDDIT_DEFAULT_TOKEN_SECONDS
        self._token_expires_at = self._clock() + max(
            expires_in - REDDIT_TOKEN_SAFETY_SECONDS,
            0,
        )
        return token.access_token

    def _invalidate_token(self, rejected: str) -> None:
        with self._token_lock:
            if self._token == rejected:
                self._token = None
                self._token_expires_at = 0.0

    def _credential(self, name: str) -> str:
        environment = self._environment if self._environment is not None else os.environ
        return environment.get(name, "").strip()

    def _user_agent(self) -> str:
        return self._credential(REDDIT_USER_AGENT_ENV) or REDDIT_DEFAULT_USER_AGENT

    def _get(
        self,
        url: str,
        params: Mapping[str, str | int],
        token: str,
    ) -> Any:  # noqa: ANN401
        headers = {
            "Authorization": f"bearer {token}",
            "User-Agent": self._user_agent(),
        }
        if self._session is not None:
            return self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=self._timeout,
                stream=True,
            )
        return requests.get(
            url,
            params=params,
            headers=headers,
            timeout=self._timeout,
            stream=True,
        )

    def _post_token(
        self,
        client_id: str,
        client_secret: str,
        user_agent: str,
    ) -> Any:  # noqa: ANN401
        data = {"grant_type": "client_credentials"}
        auth = (client_id, client_secret)
        headers = {"User-Agent": user_agent}
        if self._session is not None:
            return self._session.post(
                REDDIT_TOKEN_URL,
                data=data,
                auth=auth,
                headers=headers,
                timeout=self._timeout,
                stream=True,
            )
        return requests.post(
            REDDIT_TOKEN_URL,
            data=data,
            auth=auth,
            headers=headers,
            timeout=self._timeout,
            stream=True,
        )

    def _read_content(self, response: Any) -> bytes:  # noqa: ANN401
        return read_bounded_content(
            response,
            label=REDDIT_LABEL,
            max_bytes=self._max_response_bytes,
            chunk_bytes=REDDIT_STREAM_CHUNK_BYTES,
        )

    def _wait_for_request_slot(self) -> None:
        """Keep every Reddit request across feeds inside the shared QPM budget."""
        with self._pace_lock:
            if self._last_request_at is not None:
                remaining = self._min_request_interval - (
                    self._clock() - self._last_request_at
                )
                if remaining > 0:
                    self._sleep(remaining)
            self._last_request_at = self._clock()


def parse_reddit_listing(content: bytes) -> RedditPage:
    """Parse a listing envelope, skipping malformed or non-post children."""
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FeedFetchError(REDDIT_LABEL, "InvalidResponse") from None
    try:
        listing = RedditListing.model_validate(payload)
    except ValidationError:
        raise FeedFetchError(REDDIT_LABEL, "InvalidResponse") from None
    posts: list[RedditPost] = []
    for child in listing.data.children:
        try:
            parsed = RedditChild.model_validate(child)
        except ValidationError:
            continue
        if parsed.kind is not None and parsed.kind != "t3":
            continue
        post = parsed.data
        if post is None or not post.name:
            continue
        posts.append(post)
    return RedditPage(posts=tuple(posts), after=listing.data.after)


def _parse_token(content: bytes) -> RedditToken:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FeedFetchError(REDDIT_LABEL, "InvalidResponse") from None
    try:
        token = RedditToken.model_validate(payload)
    except ValidationError:
        raise FeedFetchError(REDDIT_LABEL, "InvalidResponse") from None
    if not token.access_token.strip():
        raise FeedFetchError(REDDIT_LABEL, "InvalidResponse")
    return token


def _http_error(
    status_code: int,
    headers: Mapping[str, str],
) -> FeedFetchError:
    return FeedFetchError(
        REDDIT_LABEL,
        "HTTPError",
        status_code=status_code,
        retryable=status_code == 429 or 500 <= status_code < 600,
        retry_after=_retry_after(headers) if status_code == 429 else None,
    )


def _retry_after(headers: Mapping[str, str]) -> float | None:
    retry_after = parse_retry_after(headers.get("Retry-After"))
    if retry_after is not None:
        return retry_after
    # Reddit's X-Ratelimit-Reset is a remaining-seconds delta, not an epoch.
    return parse_retry_after(headers.get("X-Ratelimit-Reset"))
