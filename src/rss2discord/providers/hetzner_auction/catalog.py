"""Bounded Hetzner server-auction JSON traversal."""

import json
import logging
from collections.abc import Callable, Mapping

import requests
from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.hetzner_auction.bounds import (
    HETZNER_AUCTION_LABEL,
    HETZNER_AUCTION_URL,
    HETZNER_REQUEST_SECONDS,
    HETZNER_STREAM_CHUNK_BYTES,
    HETZNER_USER_AGENT,
    MAX_HETZNER_RESPONSE_BYTES,
    MAX_HETZNER_SERVERS,
)
from rss2discord.providers.hetzner_auction.models import (
    HetznerAuctionFilters,
    HetznerAuctionServer,
)
from rss2discord.retries import parse_retry_after
from rss2discord.transports.bounded_http import read_bounded_content

logger = logging.getLogger(__name__)

FetchBytes = Callable[[str], bytes]


class HetznerAuctionCatalogClient:
    """Fetch and filter the public Hetzner auction listing feed."""

    def __init__(self, fetch: FetchBytes | None = None) -> None:
        self._fetch = fetch or _fetch_content

    def fetch_servers(self, url: str) -> tuple[HetznerAuctionServer, ...]:
        filters = HetznerAuctionFilters.from_url(url)
        content = self._fetch(HETZNER_AUCTION_URL)
        return _parse_servers(content, filters)


def _fetch_content(url: str) -> bytes:
    try:
        with requests.get(
            url,
            headers={"User-Agent": HETZNER_USER_AGENT},
            timeout=HETZNER_REQUEST_SECONDS,
            stream=True,
        ) as response:
            try:
                response.raise_for_status()
            except requests.HTTPError:
                status_code = response.status_code
                raise FeedFetchError(
                    HETZNER_AUCTION_LABEL,
                    "HTTPError",
                    status_code=status_code,
                    retryable=status_code == 429 or 500 <= status_code < 600,
                    retry_after=parse_retry_after(
                        response.headers.get("Retry-After"),
                    ),
                ) from None
            return read_bounded_content(
                response,
                label=HETZNER_AUCTION_LABEL,
                max_bytes=MAX_HETZNER_RESPONSE_BYTES,
                chunk_bytes=HETZNER_STREAM_CHUNK_BYTES,
            )
    except FeedFetchError:
        raise
    except (requests.ConnectionError, requests.Timeout) as error:
        raise FeedFetchError(
            HETZNER_AUCTION_LABEL,
            type(error).__name__,
            retryable=True,
        ) from None
    except requests.RequestException as error:
        raise FeedFetchError(
            HETZNER_AUCTION_LABEL,
            type(error).__name__,
        ) from None


def _parse_servers(
    content: bytes,
    filters: HetznerAuctionFilters,
) -> tuple[HetznerAuctionServer, ...]:
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidResponse") from None
    if not isinstance(payload, Mapping):
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidResponse")
    raw_servers = payload.get("server")
    if not isinstance(raw_servers, list):
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidResponse")
    if len(raw_servers) > MAX_HETZNER_SERVERS:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "ResponseTooLarge")
    if not raw_servers:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "EmptyResponse")

    valid = 0
    matched: list[HetznerAuctionServer] = []
    for raw_server in raw_servers:
        if not isinstance(raw_server, Mapping):
            continue
        try:
            server = HetznerAuctionServer.model_validate(raw_server)
        except ValidationError:
            continue
        valid += 1
        if filters.matches(server):
            matched.append(server)
    if valid == 0:
        raise FeedFetchError(HETZNER_AUCTION_LABEL, "InvalidResponse")
    logger.info(
        "Hetzner auction listings=%d valid=%d matched=%d",
        len(raw_servers),
        valid,
        len(matched),
    )
    return tuple(matched)
