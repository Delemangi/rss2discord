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


def test_overfull_leaf_page_extra_identical_card_is_deduped(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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

    products = fetch_catalog()

    assert {product.id for product in products} == set(range(1, 6))
    assert len(products) == 5
    assert source.visits[((0, 2), 1)] == 3
    assert "expected=3 observed=4" in caplog.text
    assert "continuing best-effort scan" in caplog.text


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
    ("total_hits", "total_pages", "nonempty", "warning"),
    [
        pytest.param(0, 0, False, False, id="empty-zero-reset"),
        pytest.param(3, 1, False, False, id="stable-empty-terminal"),
        pytest.param(3, 1, True, True, id="identical-last-card-is-not-empty"),
        pytest.param(0, 0, True, True, id="nonempty-zero-reset"),
        pytest.param(0, 1, False, True, id="mixed-zero-hits"),
        pytest.param(3, 0, False, True, id="mixed-zero-pages"),
        pytest.param(2, 1, False, True, id="other-count"),
    ],
)
def test_terminal_envelope_drift_is_logged_not_fatal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    total_hits: int,
    total_pages: int,
    *,
    nonempty: bool,
    warning: bool,
) -> None:
    # Native terminal metadata may drift or reset; best-effort scanning logs it
    # and continues, relying on final coverage rather than sentinel strictness.
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

    products = fetch_catalog()
    assert {p.id for p in products} == set(range(1, 6))
    assert len(products) == 5
    assert source.calls[-3:] == [(None, 1), ((0, 2), 1), ((2, 4), 1)]
    if warning:
        assert "sentinel page mismatch" in caplog.text
        assert "continuing best-effort scan" in caplog.text
    assert ((0, 2), 2) in source.calls


@pytest.mark.parametrize(
    ("reset_visit", "expected_success"),
    [(2, False), (3, True)],
    ids=["enumeration", "post-count"],
)
def test_zero_reset_is_logged_and_coverage_decides(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    reset_visit: int,
    *,
    expected_success: bool,
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

    if expected_success:
        products = fetch_catalog()
        assert {product.id for product in products} == set(range(1, 6))
        assert "leaf count changed" in caplog.text
    else:
        with pytest.raises(FeedFetchError, match="IncompleteCatalog") as error:
            fetch_catalog()
        assert error.value.retryable is True
        assert "coverage below minimum" in caplog.text

    assert source.visits[((0, 2), 1)] >= reset_visit


@pytest.mark.parametrize(
    ("defect", "expected_success"),
    [
        ("missing", False),
        ("root_changed", True),
        ("leaf_changed", True),
    ],
)
def test_empty_zero_terminal_does_not_bypass_final_coverage(
    monkeypatch: pytest.MonkeyPatch,
    defect: str,
    *,
    expected_success: bool,
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

    if expected_success:
        products = fetch_catalog()
        assert {product.id for product in products} == set(range(1, 6))
    else:
        with pytest.raises(FeedFetchError) as error:
            fetch_catalog()
        assert error.value.retryable is True
        assert error.value.cause_type == "IncompleteCatalog"

    assert source.visits[(None, 1)] == 2
    if defect != "root_changed":
        assert source.visits[((0, 2), 1)] == source.visits[((2, 4), 1)] == 3
