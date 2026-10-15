"""Native pagination boundaries through the real public catalog traversal."""

import json
from dataclasses import replace
from datetime import datetime

import pytest

from rss2discord.providers.gjirafa50 import http
from rss2discord.providers.gjirafa50.http import (
    FetchedGjirafa50Page,
    Gjirafa50PageRequest,
)
from rss2discord.providers.gjirafa50.models import Gjirafa50Product
from rss2discord.transports import FeedFetchError
from tests.gjirafa50_helpers import RecordingGet, StubResponse, catalog_payload
from tests.test_gjirafa50_overlap_contract import OverlappingStore, fetch_catalog


def test_short_leaf_recovers_boundary_id_from_other_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = OverlappingStore(defect="short_page")
    source.install(monkeypatch)

    products = fetch_catalog()

    assert len(products) == len({p.id for p in products}) == 5
    assert {p.id for p in products} == set(range(1, 6))
    assert source.calls[-3:] == [(None, 1), ((0, 2), 1), ((2, 4), 1)]
    assert ((0, 2), 2) in source.calls
    assert ((2, 4), 2) in source.calls


def test_unrecovered_short_leaf_fails_only_after_full_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = OverlappingStore()
    source.install(monkeypatch)
    original_fetch = source.fetch

    def fetch(
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        fetched = original_fetch(request, observed_at)
        if (
            request.price_range is not None
            and str(request.price_range) == "0-0,02"
            and request.page == 1
            and source.visits[((0, 2), 1)] == 2
        ):
            # ID 2 is not in the upper leaf, unlike boundary ID 3.
            fetched = replace(
                fetched,
                page=replace(
                    fetched.page,
                    products=(fetched.page.products[0], fetched.page.products[2]),
                ),
            )
        return fetched

    monkeypatch.setattr(source, "fetch", fetch)

    with pytest.raises(FeedFetchError, match="IncompleteCatalog") as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert source.calls[-3:] == [(None, 1), ((0, 2), 1), ((2, 4), 1)]
    assert source.visits[((0, 2), 1)] == source.visits[((2, 4), 1)] == 3


def test_empty_intermediate_page_does_not_stop_later_pages_or_final_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = OverlappingStore(49)
    source.install(monkeypatch, cap=25)
    original_fetch = source.fetch

    def fetch(
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        fetched = original_fetch(request, observed_at)
        if (
            request.price_range is not None
            and str(request.price_range) == "0-0,24"
            and request.page == 1
            and source.visits[((0, 24), 1)] == 2
        ):
            fetched = replace(fetched, page=replace(fetched.page, products=()))
        return fetched

    monkeypatch.setattr(source, "fetch", fetch)

    with pytest.raises(FeedFetchError, match="IncompleteCatalog") as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert source.visits[((0, 24), 2)] == 1
    assert source.visits[((0, 24), 3)] == 1
    assert source.calls[-3:] == [(None, 1), ((0, 24), 1), ((24, 48), 1)]


def test_overfull_leaf_page_fails_even_when_extra_card_is_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = OverlappingStore()
    source.install(monkeypatch)
    original_fetch = source.fetch

    def fetch(
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        fetched = original_fetch(request, observed_at)
        if (
            request.price_range is not None
            and str(request.price_range) == "0-0,02"
            and request.page == 1
            and source.visits[((0, 2), 1)] == 2
        ):
            fetched = replace(
                fetched,
                page=replace(
                    fetched.page,
                    products=(*fetched.page.products, fetched.page.products[-1]),
                ),
            )
        return fetched

    monkeypatch.setattr(source, "fetch", fetch)

    with pytest.raises(FeedFetchError) as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert source.visits[((0, 2), 1)] == 2
    assert ((0, 2), 2) not in source.calls


def test_native_json_product_count_must_equal_raw_dom_card_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(catalog_payload(2, [(1, 128)]))
    payload["productsCount"] = 2
    get = RecordingGet([StubResponse(json.dumps(payload).encode())])
    monkeypatch.setattr(http, "_create_session", lambda: get)

    with pytest.raises(FeedFetchError, match="InvalidCardinality"):
        fetch_catalog()

    assert len(get.params) == 1


@pytest.mark.parametrize(
    ("total_hits", "total_pages", "nonempty", "accepted"),
    [
        pytest.param(0, 0, False, True, id="empty-zero-reset"),
        pytest.param(3, 1, False, True, id="stable-empty-terminal"),
        pytest.param(3, 1, True, False, id="identical-last-card-is-not-empty"),
        pytest.param(0, 0, True, False, id="nonempty-zero-reset"),
        pytest.param(0, 1, False, False, id="mixed-zero-hits"),
        pytest.param(3, 0, False, False, id="mixed-zero-pages"),
        pytest.param(2, 1, False, False, id="other-count"),
    ],
)
def test_terminal_envelope_is_narrowly_validated(
    monkeypatch: pytest.MonkeyPatch,
    total_hits: int,
    total_pages: int,
    *,
    nonempty: bool,
    accepted: bool,
) -> None:
    # Native terminal metadata may reset, but only an empty terminal gets this
    # exception; successful return still requires all final reconciliation.
    source = OverlappingStore()
    source.install(monkeypatch)
    original_fetch = source.fetch
    last_card: tuple[Gjirafa50Product, ...] = ()

    def fetch(
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        nonlocal last_card
        fetched = original_fetch(request, observed_at)
        if request.price_range is not None and str(request.price_range) == "0-0,02":
            if request.page == 1:
                last_card = fetched.page.products[-1:]
            elif request.page == 2:
                fetched = replace(
                    fetched,
                    page=replace(
                        fetched.page,
                        total_hits=total_hits,
                        total_pages=total_pages,
                        products=last_card if nonempty else (),
                    ),
                )
        return fetched

    monkeypatch.setattr(source, "fetch", fetch)

    if accepted:
        products = fetch_catalog()
        assert {p.id for p in products} == set(range(1, 6))
        assert len(products) == 5
        assert source.calls[-3:] == [(None, 1), ((0, 2), 1), ((2, 4), 1)]
    else:
        with pytest.raises(FeedFetchError, match="InvalidTerminalPage") as error:
            fetch_catalog()
        assert error.value.retryable is True
    assert ((0, 2), 2) in source.calls


@pytest.mark.parametrize("reset_visit", [2, 3], ids=["enumeration", "post-count"])
def test_zero_reset_is_never_accepted_outside_terminal_page(
    monkeypatch: pytest.MonkeyPatch,
    reset_visit: int,
) -> None:
    source = OverlappingStore()
    source.install(monkeypatch)
    original_fetch = source.fetch

    def fetch(
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        fetched = original_fetch(request, observed_at)
        if (
            request.price_range is not None
            and str(request.price_range) == "0-0,02"
            and request.page == 1
            and source.visits[((0, 2), 1)] == reset_visit
        ):
            fetched = replace(
                fetched,
                page=replace(fetched.page, total_hits=0, total_pages=0, products=()),
            )
        return fetched

    monkeypatch.setattr(source, "fetch", fetch)

    with pytest.raises(FeedFetchError) as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert source.visits[((0, 2), 1)] == reset_visit


@pytest.mark.parametrize("defect", ["missing", "root_changed", "leaf_changed"])
def test_empty_zero_terminal_does_not_bypass_final_reconciliation(
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
) -> None:
    source = OverlappingStore(defect=defect)
    source.install(monkeypatch)
    original_fetch = source.fetch

    def fetch(
        request: Gjirafa50PageRequest,
        observed_at: datetime,
    ) -> FetchedGjirafa50Page:
        fetched = original_fetch(request, observed_at)
        if request.price_range is not None and request.page == 2:
            fetched = replace(
                fetched,
                page=replace(fetched.page, total_hits=0, total_pages=0, products=()),
            )
        return fetched

    monkeypatch.setattr(source, "fetch", fetch)

    with pytest.raises(FeedFetchError) as error:
        fetch_catalog()

    assert error.value.retryable is True
    assert error.value.cause_type != "InvalidTerminalPage"
    assert source.visits[(None, 1)] == 2
    if defect != "root_changed":
        assert source.visits[((0, 2), 1)] == source.visits[((2, 4), 1)] == 3
