import json
from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from decimal import Decimal

import requests
from pydantic import JsonValue

from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.setec_catalog_bounds import (
    SETEC_COUNT_FIELD,
    SETEC_PRICE_FIELD,
    SETEC_SEARCH_PAGE_SIZE,
)

CATALOG_URL = "https://setec.mk/e-prodazba"

# Payload amounts are serialized with json.dumps, so Decimal is deliberately excluded.
type Amount = int | float | str


def no_wait_fetch_retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def catalog_scan_should_stop() -> bool:
    return False


def product_payload(
    product_id: str,
    handle: str,
    *,
    price: Amount = 1_499,
    original_price: Amount = 1_999,
) -> dict[str, JsonValue]:
    """Build one hit shaped like the display projection the client requests."""
    return {
        "id": product_id,
        "title": f"Product {product_id}",
        "handle": handle,
        "thumbnail": f"https://cdn.setec.mk/{product_id}.webp",
        "created_at": "2026-07-23T02:24:28.424Z",
        "variants": [
            {
                "calculated_price": {
                    "calculated_amount": price,
                    "original_amount": original_price,
                    "currency_code": "mkd",
                },
            },
        ],
        "product_categories": [{"name": "Computers"}, {"name": "Accessories"}],
    }


def price_payload(product_id: str, *, price: Amount = 1_499) -> dict[str, JsonValue]:
    """Build one hit shaped like the identifier-and-price projection."""
    return {
        "id": product_id,
        "variants": [
            {
                "calculated_price": {
                    "calculated_amount": price,
                    "currency_code": "mkd",
                },
            },
        ],
    }


def search_payload(hits: Sequence[dict[str, JsonValue]]) -> bytes:
    """Encode a hits-carrying search response."""
    return json.dumps({"hits": list(hits)}).encode()


def count_payload(amounts: Sequence[Amount]) -> bytes:
    """Encode a facet-only response, reproducing the empty-bucket asymmetry.

    The live index always returns ``facetDistribution`` with the requested field
    present (empty when nothing matched) but omits the field from ``facetStats``
    entirely for an empty bucket, so a zero count must never read facet stats.

    The single-valued count field carries one entry per document while the price
    field carries one per distinct amount, mirroring how the live index counts a
    multi-valued facet once per value rather than once per document.
    """
    distribution = Counter(str(amount) for amount in amounts)
    numeric_amounts = [float(Decimal(str(amount))) for amount in amounts]
    facet_stats: dict[str, JsonValue] = {}
    if numeric_amounts:
        facet_stats = {
            SETEC_PRICE_FIELD: {
                "min": min(numeric_amounts),
                "max": max(numeric_amounts),
            },
        }
    count_distribution: dict[str, int] = {"published": len(amounts)} if amounts else {}
    return json.dumps(
        {
            "hits": [],
            "estimatedTotalHits": min(len(amounts), SETEC_SEARCH_PAGE_SIZE),
            "facetDistribution": {
                SETEC_COUNT_FIELD: count_distribution,
                SETEC_PRICE_FIELD: dict(distribution),
            },
            "facetStats": facet_stats,
        },
    ).encode()


@dataclass(frozen=True, slots=True)
class StubResponse:
    content: bytes
    status_code: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        if self.chunks is None:
            yield self.content
            return
        yield from self.chunks


class RecordingPost:
    """Return queued responses while recording mutable request history."""

    def __init__(self, responses: list[StubResponse]) -> None:
        self.responses: list[StubResponse] = responses
        self.urls: list[str] = []
        self.bodies: list[Mapping[str, JsonValue]] = []
        self.headers: list[Mapping[str, str]] = []
        self.allow_redirects: list[bool] = []

    def __call__(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del timeout, stream
        self.urls.append(url)
        self.bodies.append(json)
        self.headers.append(headers)
        self.allow_redirects.append(allow_redirects)
        return nullcontext(self.responses.pop(0))

    @property
    def filters(self) -> list[JsonValue]:
        """Return the filter of every recorded request, in order."""
        return [body.get("filter") for body in self.bodies]


@dataclass(frozen=True, slots=True)
class RaisingPost:
    error: requests.RequestException

    def __call__(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del url, json, headers, timeout, stream, allow_redirects
        raise self.error


@dataclass(frozen=True, slots=True)
class IndexedProduct:
    """One product in the fake index, addressed by identifier and amount."""

    product_id: str
    amount: Amount

    @property
    def numeric_amount(self) -> float:
        return float(Decimal(str(self.amount)))


class FakeMeilisearchIndex:
    """Answer band counts and hit fetches from an in-memory corpus.

    Reproduces the two live behaviours the client depends on: facet counts are
    exact and uncapped, while a hits request is silently truncated at the page
    size rather than erroring.
    """

    def __init__(
        self,
        products: Sequence[IndexedProduct],
        *,
        page_size: int = SETEC_SEARCH_PAGE_SIZE,
    ) -> None:
        self._products = tuple(products)
        self._page_size = page_size
        self.bodies: list[Mapping[str, JsonValue]] = []

    def __call__(
        self,
        url: str,
        *,
        json: Mapping[str, JsonValue],
        headers: Mapping[str, str],
        timeout: int,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, stream, allow_redirects
        self.bodies.append(json)
        matched = self._matching(json.get("filter"))
        if json.get("limit") == 0 and "facets" in json:
            return nullcontext(
                StubResponse(count_payload([item.amount for item in matched])),
            )
        limit = json.get("limit")
        page_limit = (
            min(int(limit), self._page_size)
            if isinstance(limit, int)
            else self._page_size
        )
        hits = [price_payload(item.product_id, price=item.amount) for item in matched]
        return nullcontext(StubResponse(search_payload(hits[:page_limit])))

    @property
    def filters(self) -> list[JsonValue]:
        return [body.get("filter") for body in self.bodies]

    @property
    def count_request_total(self) -> int:
        return sum(1 for body in self.bodies if body.get("limit") == 0)

    @property
    def hit_request_total(self) -> int:
        return sum(1 for body in self.bodies if body.get("limit") != 0)

    def _matching(self, search_filter: JsonValue) -> list[IndexedProduct]:
        if not isinstance(search_filter, list):
            return list(self._products)
        low, high = _parse_band(search_filter)
        return [
            item
            for item in self._products
            if (low is None or item.numeric_amount >= low)
            and (high is None or item.numeric_amount < high)
        ]


def _parse_band(
    search_filter: Sequence[JsonValue],
) -> tuple[float | None, float | None]:
    low: float | None = None
    high: float | None = None
    for clause in search_filter:
        if not isinstance(clause, str) or not clause.startswith(SETEC_PRICE_FIELD):
            continue
        remainder = clause.removeprefix(SETEC_PRICE_FIELD).strip()
        if remainder.startswith(">="):
            low = float(remainder.removeprefix(">=").strip())
        elif remainder.startswith("<"):
            high = float(remainder.removeprefix("<").strip())
    return low, high
