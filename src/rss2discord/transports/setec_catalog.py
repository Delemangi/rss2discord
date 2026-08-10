"""Bounded price-index, product-lookup and latest-window Setec search scans."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel, JsonValue, ValidationError

from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import setec_catalog_bounds as bounds
from rss2discord.transports.base import FeedFetchError
from rss2discord.transports.setec_catalog_bounds import (
    MAX_SETEC_BAND_DEPTH,
    MAX_SETEC_CATALOG_PRODUCTS,
    MAX_SETEC_CATALOG_RESPONSE_BYTES,
    MAX_SETEC_LATEST_RESPONSE_BYTES,
    MAX_SETEC_REDIRECTS,
    MAX_SETEC_SEARCH_REQUESTS,
    SETEC_COUNT_FIELD,
    SETEC_CREATED_AT_SORT,
    SETEC_DISPLAY_PROJECTION,
    SETEC_LABEL,
    SETEC_PRICE_FIELD,
    SETEC_PRICE_PROJECTION,
    SETEC_PRODUCT_LOOKUP_BATCH_SIZE,
    SETEC_SEARCH_PAGE_SIZE,
    SETEC_WINDOW_SIZE,
    SetecSearchRequest,
)
from rss2discord.transports.setec_http import SetecSearchClient
from rss2discord.transports.setec_models import (
    SetecCountResponse,
    SetecPriceEntry,
    SetecPriceIndexResponse,
    SetecProduct,
    SetecProductResponse,
    SetecRawHitsResponse,
)


@dataclass(slots=True)
class _SearchBudget:
    """Mutable request and response-byte budget for one scan attempt."""

    consumed_bytes: int = 0
    request_count: int = 0


@dataclass(frozen=True, slots=True)
class _SearchSession:
    """One scan attempt's endpoint, client and shared budget."""

    search_url: str
    client: SetecSearchClient
    budget: _SearchBudget


@dataclass(frozen=True, slots=True)
class _PriceBand:
    """A half-open [low, high) amount range, unbounded above when high is None."""

    low: float
    high: float | None = None

    def as_filter(self) -> list[str]:
        """Return the search filter selecting exactly this half-open range."""
        if self.high is None:
            return [f"{SETEC_PRICE_FIELD} >= {self.low}"]
        return [
            f"{SETEC_PRICE_FIELD} >= {self.low}",
            f"{SETEC_PRICE_FIELD} < {self.high}",
        ]

    def split(self) -> tuple[_PriceBand, _PriceBand] | None:
        """Halve the range, or return None when it can no longer be divided."""
        if self.high is None:
            return None
        middle = self.low + (self.high - self.low) / 2
        if middle <= self.low or middle >= self.high:
            return None
        return _PriceBand(self.low, middle), _PriceBand(middle, self.high)


@dataclass(frozen=True, slots=True)
class _PriceIndexScan:
    """Collaborators and accumulator shared by one price-index recursion."""

    session: _SearchSession
    entries: dict[str, SetecPriceEntry]
    is_shutdown_requested: Callable[[], bool]


class SetecCatalogClient:
    """Enumerate Setec prices and products with bounded search traversal."""

    def fetch_latest_products(self, url: str) -> tuple[SetecProduct, ...]:
        """Fetch the newest window of products in ascending creation order."""
        client = SetecSearchClient()
        search_url = client.build_search_url(url)
        fetched = client.search(
            search_url,
            SetecSearchRequest(
                body={
                    "q": "",
                    "limit": SETEC_WINDOW_SIZE,
                    "sort": [SETEC_CREATED_AT_SORT],
                    "attributesToRetrieve": list(SETEC_DISPLAY_PROJECTION),
                },
                max_single_response_bytes=MAX_SETEC_LATEST_RESPONSE_BYTES,
                remaining_scan_response_bytes=(
                    MAX_SETEC_LATEST_RESPONSE_BYTES * (MAX_SETEC_REDIRECTS + 1)
                ),
            ),
        )
        response = _validate(SetecProductResponse, fetched.content)
        if len(response.hits) > SETEC_WINDOW_SIZE:
            raise FeedFetchError(SETEC_LABEL, "PageCardinalityExceeded")
        return tuple(reversed(response.hits))

    def fetch_price_index(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]:
        """Fetch every priced product's id and amount in ascending price-band order."""
        client = SetecSearchClient()
        search_url = client.build_search_url(url)
        return retry_policy.execute(
            lambda: self._scan_price_index(
                search_url,
                client,
                is_shutdown_requested,
            ),
        )

    def fetch_products_by_ids(
        self,
        url: str,
        product_ids: Sequence[str],
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        """Fetch the display projection for the given product identifiers only."""
        if not product_ids:
            return ()
        client = SetecSearchClient()
        search_url = client.build_search_url(url)
        return retry_policy.execute(
            lambda: self._scan_products_by_ids(
                search_url,
                client,
                product_ids,
                is_shutdown_requested,
            ),
        )

    def _scan_price_index(
        self,
        search_url: str,
        client: SetecSearchClient,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecPriceEntry, ...]:
        session = _SearchSession(search_url, client, _SearchBudget())
        if is_shutdown_requested():
            raise FeedFetchInterruptedError
        summary = self._summarize(session)
        declared_count = summary.count_for(SETEC_COUNT_FIELD)
        if declared_count is None:
            raise FeedFetchError(SETEC_LABEL, "InvalidResponse")
        if declared_count > MAX_SETEC_CATALOG_PRODUCTS:
            raise FeedFetchError(SETEC_LABEL, "ProductLimitExceeded")
        price_bounds = summary.bounds_for(SETEC_PRICE_FIELD)
        if declared_count == 0:
            # Amount bounds without a counted document contradict each other, so
            # an empty catalog is only believed when neither facet saw anything.
            if price_bounds is not None:
                raise FeedFetchError(SETEC_LABEL, "InvalidResponse")
            return ()
        if price_bounds is None:
            raise FeedFetchError(SETEC_LABEL, "InvalidResponse")
        scan = _PriceIndexScan(session, {}, is_shutdown_requested)
        self._collect_band(scan, _PriceBand(0.0, price_bounds.maximum + 1.0), 0)
        if len(scan.entries) != declared_count:
            raise FeedFetchError(SETEC_LABEL, "IncompleteCatalog", retryable=True)
        return tuple(scan.entries.values())

    def _collect_band(
        self,
        scan: _PriceIndexScan,
        band: _PriceBand,
        depth: int,
    ) -> None:
        if scan.is_shutdown_requested():
            raise FeedFetchInterruptedError
        band_count = self._count_band(scan.session, band)
        if band_count == 0:
            return
        if band_count <= SETEC_SEARCH_PAGE_SIZE:
            self._collect_band_entries(scan, band, band_count)
            return
        halves = band.split() if depth < MAX_SETEC_BAND_DEPTH else None
        if halves is None:
            raise FeedFetchError(SETEC_LABEL, "PriceBandUnsplittable")
        lower_half, upper_half = halves
        self._collect_band(scan, lower_half, depth + 1)
        self._collect_band(scan, upper_half, depth + 1)

    def _collect_band_entries(
        self,
        scan: _PriceIndexScan,
        band: _PriceBand,
        band_count: int,
    ) -> None:
        fetched = self._run_search(
            scan.session,
            {
                "q": "",
                "limit": SETEC_SEARCH_PAGE_SIZE,
                "filter": band.as_filter(),
                "attributesToRetrieve": list(SETEC_PRICE_PROJECTION),
            },
        )
        hits = _validate(SetecPriceIndexResponse, fetched).hits
        if len(hits) > SETEC_SEARCH_PAGE_SIZE:
            raise FeedFetchError(SETEC_LABEL, "PageCardinalityExceeded")
        if len(hits) != band_count:
            raise FeedFetchError(SETEC_LABEL, "IncompleteCatalog", retryable=True)
        for entry in hits:
            existing_entry = scan.entries.get(entry.id)
            if existing_entry is None:
                if len(scan.entries) >= MAX_SETEC_CATALOG_PRODUCTS:
                    raise FeedFetchError(SETEC_LABEL, "ProductLimitExceeded")
                scan.entries[entry.id] = entry
                continue
            if existing_entry != entry:
                # A price edited mid-scan lands the product in two bands with
                # different values, which a restart from the summary resolves.
                raise FeedFetchError(
                    SETEC_LABEL,
                    "DuplicateProductId",
                    retryable=True,
                )

    def _scan_products_by_ids(
        self,
        search_url: str,
        client: SetecSearchClient,
        product_ids: Sequence[str],
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[SetecProduct, ...]:
        session = _SearchSession(search_url, client, _SearchBudget())
        products: list[SetecProduct] = []
        seen_products: dict[str, SetecProduct] = {}
        for batch in _batched(product_ids, SETEC_PRODUCT_LOOKUP_BATCH_SIZE):
            if is_shutdown_requested():
                raise FeedFetchInterruptedError
            fetched = self._run_search(
                session,
                {
                    "q": "",
                    "limit": len(batch),
                    "filter": [_identifier_filter(batch)],
                    "attributesToRetrieve": list(SETEC_DISPLAY_PROJECTION),
                },
            )
            hits = _validate(SetecRawHitsResponse, fetched).hits
            if len(hits) > len(batch):
                raise FeedFetchError(SETEC_LABEL, "PageCardinalityExceeded")
            for hit in hits:
                product = _validate_product(hit)
                if product is None:
                    continue
                existing_product = seen_products.get(product.id)
                if existing_product is None:
                    seen_products[product.id] = product
                    products.append(product)
                    continue
                if existing_product != product:
                    raise FeedFetchError(SETEC_LABEL, "DuplicateProductId")
        return tuple(products)

    def _summarize(self, session: _SearchSession) -> SetecCountResponse:
        """Read the whole catalog's exact size and its calculated-amount bounds."""
        fetched = self._run_search(
            session,
            {
                "q": "",
                "limit": 0,
                "filter": _PriceBand(0.0).as_filter(),
                "facets": [SETEC_COUNT_FIELD, SETEC_PRICE_FIELD],
            },
        )
        return _validate(SetecCountResponse, fetched)

    def _count_band(self, session: _SearchSession, band: _PriceBand) -> int:
        """Return the exact document count for one band without fetching it."""
        fetched = self._run_search(
            session,
            {
                "q": "",
                "limit": 0,
                "filter": band.as_filter(),
                "facets": [SETEC_COUNT_FIELD],
            },
        )
        band_count = _validate(SetecCountResponse, fetched).count_for(SETEC_COUNT_FIELD)
        if band_count is None:
            raise FeedFetchError(SETEC_LABEL, "InvalidResponse")
        return band_count

    def _run_search(self, session: _SearchSession, body: dict[str, object]) -> bytes:
        if session.budget.consumed_bytes >= bounds.MAX_SETEC_CATALOG_SCAN_BYTES:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
        if session.budget.request_count >= MAX_SETEC_SEARCH_REQUESTS:
            raise FeedFetchError(SETEC_LABEL, "SearchRequestLimitExceeded")
        session.budget.request_count += 1
        fetched = session.client.search(
            session.search_url,
            SetecSearchRequest(
                body=body,
                max_single_response_bytes=MAX_SETEC_CATALOG_RESPONSE_BYTES,
                remaining_scan_response_bytes=(
                    bounds.MAX_SETEC_CATALOG_SCAN_BYTES - session.budget.consumed_bytes
                ),
            ),
        )
        session.budget.consumed_bytes += fetched.response_bytes
        return fetched.content


def _identifier_filter(product_ids: Sequence[str]) -> str:
    quoted_ids = ",".join(json.dumps(product_id) for product_id in product_ids)
    return f"id IN [{quoted_ids}]"


def _batched(items: Sequence[str], batch_size: int) -> Iterable[Sequence[str]]:
    for start in range(0, len(items), batch_size):
        yield items[start : start + batch_size]


def _validate[ModelT: BaseModel](model: type[ModelT], content: bytes) -> ModelT:
    try:
        return model.model_validate_json(content)
    except ValidationError:
        raise FeedFetchError(SETEC_LABEL, "InvalidResponse") from None


def _validate_product(hit: JsonValue) -> SetecProduct | None:
    """Validate one looked-up product, skipping it when it is unusable.

    A single malformed document is treated exactly like an absent one so it
    cannot discard the other products sharing its batch.
    """
    try:
        return SetecProduct.model_validate(hit)
    except ValidationError:
        return None
