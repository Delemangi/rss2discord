from datetime import datetime

import pytest

from rss2discord.models import EntryId
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports import FeedFetchError, pazar3_catalog
from rss2discord.transports.pazar3_models import Pazar3Page
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from rss2discord.transports.pazar3_scope import Pazar3PageRequest
from tests.pazar3_helpers import FIXED_NOW, SEARCH_URL
from tests.test_pazar3_strategy import listing


def retry_policy() -> FetchRetryPolicy:
    return FetchRetryPolicy(
        sleep=lambda seconds: True,
        on_retry=lambda error, delay: None,
    )


def clock() -> datetime:
    return FIXED_NOW


def test_pazar3_catalog_traverses_complete_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = iter(
        (
            Pazar3Page(
                tuple(listing(str(entry_id), minutes=2) for entry_id in range(2, 52)),
                frozenset(EntryId(str(entry_id)) for entry_id in range(2, 52)),
                50,
                51,
                False,
            ),
            Pazar3Page(
                (listing("1", minutes=1),),
                frozenset({EntryId("1")}),
                1,
                51,
                True,
            ),
        ),
    )
    requested: list[int] = []

    def fetch(request: Pazar3PageRequest, *_args: object) -> bytes:
        requested.append(request.page)
        return b"page"

    monkeypatch.setattr(pazar3_catalog, "fetch_pazar3_page", fetch)
    monkeypatch.setattr(pazar3_catalog, "parse_pazar3_page", lambda *_args: next(pages))
    client = pazar3_catalog.Pazar3CatalogClient(
        Pazar3RequestPacer(lambda: 0.0),
        lambda _seconds: True,
        clock=clock,
    )

    products = client.fetch_catalog(
        SEARCH_URL,
        retry_policy=retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    assert requested == [1, 2]
    assert len(products) == 51
    assert products[-1].entry_id == "1"


def test_pazar3_catalog_rejects_scope_above_product_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Pazar3Page(
        (listing("1", minutes=1),),
        frozenset({EntryId("1")}),
        1,
        501,
        False,
    )
    monkeypatch.setattr(pazar3_catalog, "fetch_pazar3_page", lambda *_args: b"page")
    monkeypatch.setattr(pazar3_catalog, "parse_pazar3_page", lambda *_args: page)
    client = pazar3_catalog.Pazar3CatalogClient(
        Pazar3RequestPacer(lambda: 0.0),
        lambda _seconds: True,
        clock=clock,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        client.fetch_catalog(
            SEARCH_URL,
            retry_policy=retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "ProductLimitExceeded"


def test_pazar3_catalog_rejects_changed_result_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt = (
        Pazar3Page(
            (listing("2", minutes=2),),
            frozenset({EntryId("2")}),
            50,
            51,
            False,
        ),
        Pazar3Page(
            (listing("1", minutes=1),),
            frozenset({EntryId("1")}),
            1,
            50,
            True,
        ),
    )
    pages = iter(attempt * 3)
    monkeypatch.setattr(pazar3_catalog, "fetch_pazar3_page", lambda *_args: b"page")
    monkeypatch.setattr(pazar3_catalog, "parse_pazar3_page", lambda *_args: next(pages))
    client = pazar3_catalog.Pazar3CatalogClient(
        Pazar3RequestPacer(lambda: 0.0),
        lambda _seconds: True,
        clock=clock,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        client.fetch_catalog(
            SEARCH_URL,
            retry_policy=retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "ChangedResultCount"
    assert fetch_error.value.retryable


def test_pazar3_catalog_retry_restarts_from_page_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = iter(
        (
            Pazar3Page(
                (listing("2", minutes=2),),
                frozenset({EntryId("2")}),
                50,
                51,
                False,
            ),
            Pazar3Page(
                (listing("1", minutes=1),),
                frozenset({EntryId("1")}),
                1,
                50,
                True,
            ),
            Pazar3Page(
                (listing("3", minutes=1),),
                frozenset({EntryId("3")}),
                1,
                1,
                True,
            ),
        ),
    )
    requested: list[int] = []

    def fetch(request: Pazar3PageRequest, *_args: object) -> bytes:
        requested.append(request.page)
        return b"page"

    monkeypatch.setattr(pazar3_catalog, "fetch_pazar3_page", fetch)
    monkeypatch.setattr(pazar3_catalog, "parse_pazar3_page", lambda *_args: next(pages))
    client = pazar3_catalog.Pazar3CatalogClient(
        Pazar3RequestPacer(lambda: 0.0),
        lambda _seconds: True,
        clock=clock,
    )

    products = client.fetch_catalog(
        SEARCH_URL,
        retry_policy=retry_policy(),
        is_shutdown_requested=lambda: False,
    )

    assert requested == [1, 2, 1]
    assert [product.entry_id for product in products] == ["3"]


def test_pazar3_catalog_rejects_missing_terminal_page_at_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Pazar3Page(
        (listing("1", minutes=1),),
        frozenset({EntryId("1")}),
        50,
        51,
        False,
    )
    monkeypatch.setattr(pazar3_catalog, "MAX_PAZAR3_CATALOG_PAGES", 1)
    monkeypatch.setattr(pazar3_catalog, "fetch_pazar3_page", lambda *_args: b"page")
    monkeypatch.setattr(pazar3_catalog, "parse_pazar3_page", lambda *_args: page)
    client = pazar3_catalog.Pazar3CatalogClient(
        Pazar3RequestPacer(lambda: 0.0),
        lambda _seconds: True,
        clock=clock,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        client.fetch_catalog(
            SEARCH_URL,
            retry_policy=retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "PageLimitExceeded"


def test_pazar3_catalog_retries_partial_cross_page_overlap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_page = Pazar3Page(
        tuple(listing(str(entry_id), minutes=2) for entry_id in range(1, 51)),
        frozenset(EntryId(str(entry_id)) for entry_id in range(1, 51)),
        50,
        100,
        False,
    )
    second_page = Pazar3Page(
        tuple(listing(str(entry_id), minutes=1) for entry_id in range(50, 100)),
        frozenset(EntryId(str(entry_id)) for entry_id in range(50, 100)),
        50,
        100,
        True,
    )
    pages = iter((first_page, second_page) * 3)
    requested: list[int] = []

    def fetch(request: Pazar3PageRequest, *_args: object) -> bytes:
        requested.append(request.page)
        return b"page"

    monkeypatch.setattr(pazar3_catalog, "fetch_pazar3_page", fetch)
    monkeypatch.setattr(pazar3_catalog, "parse_pazar3_page", lambda *_args: next(pages))
    client = pazar3_catalog.Pazar3CatalogClient(
        Pazar3RequestPacer(lambda: 0.0),
        lambda _seconds: True,
        clock=clock,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        client.fetch_catalog(
            SEARCH_URL,
            retry_policy=retry_policy(),
            is_shutdown_requested=lambda: False,
        )

    assert fetch_error.value.cause_type == "PaginationCycle"
    assert fetch_error.value.retryable
    assert requested == [1, 2, 1, 2, 1, 2]
