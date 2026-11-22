"""Bounded Hetzner Cloud server-type catalog client."""

import json
import os
import re
from collections.abc import Callable, Mapping
from typing import Final
from urllib.parse import parse_qs, urlsplit

import requests
from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.hetzner_cloud.bounds import (
    HETZNER_CLOUD_HOST,
    HETZNER_CLOUD_LABEL,
    HETZNER_CLOUD_REQUEST_SECONDS,
    HETZNER_CLOUD_SERVER_TYPES_PATH,
    HETZNER_CLOUD_STREAM_CHUNK_BYTES,
    HETZNER_CLOUD_TOKEN_ENV,
    HETZNER_CLOUD_URL,
    HETZNER_CLOUD_USER_AGENT,
    MAX_HETZNER_CLOUD_RESPONSE_BYTES,
)
from rss2discord.providers.hetzner_cloud.models import (
    HetznerCloudFilters,
    HetznerCloudOffer,
    HetznerCloudServerType,
    HetznerCloudServerTypesResponse,
)
from rss2discord.retries import (
    FeedFetchInterruptedError,
    FetchRetryPolicy,
    parse_retry_after,
)
from rss2discord.transports.bounded_http import read_bounded_content

FetchBytes = Callable[[str, str], bytes]

_ALLOWED_QUERY_KEYS: Final = frozenset({"locations", "server_types"})
_NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$", flags=re.ASCII)


class HetznerCloudCatalogClient:
    """Fetch server types with per-location availability and prices."""

    def __init__(
        self,
        fetch: FetchBytes | None = None,
        *,
        token: str | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self._fetch = fetch or _fetch_content
        self._token = token
        self._environment = environment

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy | None = None,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[HetznerCloudOffer, ...]:
        filters = validate_hetzner_cloud_url(url)
        token = self._resolved_token()
        if is_shutdown_requested():
            raise FeedFetchInterruptedError

        def operation() -> bytes:
            return self._fetch(HETZNER_CLOUD_URL, token)

        content = (
            retry_policy.execute(operation) if retry_policy is not None else operation()
        )
        return _build_offers(_parse_server_types(content), filters)

    def _resolved_token(self) -> str:
        token = self._token
        if token is None:
            environment = (
                self._environment if self._environment is not None else os.environ
            )
            token = environment.get(HETZNER_CLOUD_TOKEN_ENV, "")
        token = token.strip()
        if not token:
            raise FeedFetchError(HETZNER_CLOUD_LABEL, "MissingToken")
        return token


def validate_hetzner_cloud_url(url: str) -> HetznerCloudFilters:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidUrl") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname != HETZNER_CLOUD_HOST
        or port not in {None, 443}
        or parsed.path != HETZNER_CLOUD_SERVER_TYPES_PATH
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidUrl")
    query = parse_qs(parsed.query, keep_blank_values=True)
    if any(key not in _ALLOWED_QUERY_KEYS for key in query):
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidUrl")
    return HetznerCloudFilters(
        server_types=_query_names(query, "server_types"),
        locations=_query_names(query, "locations"),
    )


def _query_names(
    query: dict[str, list[str]],
    key: str,
) -> tuple[str, ...] | None:
    values = query.get(key)
    if values is None:
        return None
    if len(values) != 1:
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidUrl")
    names: list[str] = []
    for raw_name in values[0].split(","):
        name = raw_name.strip()
        if not name or _NAME_PATTERN.fullmatch(name) is None:
            raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidUrl")
        if name not in names:
            names.append(name)
    if not names:
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidUrl")
    return tuple(names)


def _fetch_content(url: str, token: str) -> bytes:
    try:
        with requests.get(
            url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": HETZNER_CLOUD_USER_AGENT,
            },
            timeout=HETZNER_CLOUD_REQUEST_SECONDS,
            stream=True,
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError:
                status_code = response.status_code
                if status_code == 401:
                    cause = "Unauthorized"
                elif status_code == 403:
                    cause = "Forbidden"
                else:
                    cause = "HTTPError"
                raise FeedFetchError(
                    HETZNER_CLOUD_LABEL,
                    cause,
                    status_code=status_code,
                    retryable=status_code == 429 or 500 <= status_code < 600,
                    retry_after=parse_retry_after(
                        response.headers.get("Retry-After"),
                    ),
                ) from None
            return read_bounded_content(
                response,
                label=HETZNER_CLOUD_LABEL,
                max_bytes=MAX_HETZNER_CLOUD_RESPONSE_BYTES,
                chunk_bytes=HETZNER_CLOUD_STREAM_CHUNK_BYTES,
            )
    except FeedFetchError:
        raise
    except (requests.ConnectionError, requests.Timeout) as error:
        raise FeedFetchError(
            HETZNER_CLOUD_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(
            HETZNER_CLOUD_LABEL,
            type(error).__name__,
        ) from None


def _parse_server_types(content: bytes) -> tuple[HetznerCloudServerType, ...]:
    try:
        payload = json.loads(content)
        response = HetznerCloudServerTypesResponse.model_validate(payload)
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError):
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidResponse") from None
    if not response.server_types:
        raise FeedFetchError(HETZNER_CLOUD_LABEL, "InvalidResponse")
    return response.server_types


def _build_offers(
    server_types: tuple[HetznerCloudServerType, ...],
    filters: HetznerCloudFilters,
) -> tuple[HetznerCloudOffer, ...]:
    offers: list[HetznerCloudOffer] = []
    for server_type in server_types:
        if not server_type.name:
            continue
        if (
            filters.server_types is not None
            and server_type.name not in filters.server_types
        ):
            continue
        prices = {
            price.location: price.price_monthly.gross
            for price in server_type.prices
            if price.location
        }
        for location in server_type.locations:
            if not location.name:
                continue
            if filters.locations is not None and location.name not in filters.locations:
                continue
            price = prices.get(location.name)
            if price is None or price <= 0:
                continue
            offers.append(
                HetznerCloudOffer(
                    server_type=server_type,
                    location=location,
                    price=price,
                ),
            )
    return tuple(offers)
