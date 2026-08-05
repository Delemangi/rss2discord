from datetime import timedelta

import pytest

from rss2discord.models import EntryId
from rss2discord.transports import FeedFetchError, pazar3
from rss2discord.transports.pazar3_models import Pazar3Listing, Pazar3Page
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer
from rss2discord.transports.pazar3_scope import Pazar3PageRequest
from tests.pazar3_helpers import FIXED_NOW, SEARCH_URL


def listing(entry_id: str, *, minutes: int) -> Pazar3Listing:
    return Pazar3Listing(
        entry_id=EntryId(entry_id),
        url=f"https://www.pazar3.mk/oglas/elektronika/item/{entry_id}",
        title=f"Listing {entry_id}",
        price="100 МКД",
        location="Скопjе",
        category="Електроника",
        activity_at=FIXED_NOW + timedelta(minutes=minutes),
        image_url=None,
    )


def test_pazar3_strategy_scans_three_pages_and_delivers_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = {
        1: Pazar3Page(
            (listing("1", minutes=3),),
            frozenset({EntryId("1")}),
            1,
            3,
            False,
        ),
        2: Pazar3Page(
            (listing("2", minutes=2),),
            frozenset({EntryId("2")}),
            1,
            3,
            False,
        ),
        3: Pazar3Page(
            (listing("3", minutes=1),),
            frozenset({EntryId("3")}),
            1,
            3,
            True,
        ),
    }
    requested: list[int] = []

    def fetch_page(request: Pazar3PageRequest, *_args: object) -> bytes:
        requested.append(request.page)
        return str(request.page).encode()

    monkeypatch.setattr(pazar3, "fetch_pazar3_page", fetch_page)
    monkeypatch.setattr(
        pazar3,
        "parse_pazar3_page",
        lambda html, _request, _now: pages[int(html)],
    )
    strategy = pazar3.Pazar3Strategy(
        clock=lambda: FIXED_NOW,
        pacer=Pazar3RequestPacer(lambda: 0.0),
        sleep=lambda _seconds: True,
    )

    entries, source = strategy.fetch_entries(SEARCH_URL)

    assert requested == [1, 2, 3]
    assert [entry.entry_id for entry in entries] == ["3", "2", "1"]
    assert source == "Pazar3"


def test_pazar3_strategy_initializes_all_observed_organic_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Pazar3Page((), frozenset({EntryId("42")}), 1, 1, True)
    monkeypatch.setattr(pazar3, "fetch_pazar3_page", lambda *_args: b"page")
    monkeypatch.setattr(pazar3, "parse_pazar3_page", lambda *_args: page)
    strategy = pazar3.Pazar3Strategy(
        clock=lambda: FIXED_NOW,
        pacer=Pazar3RequestPacer(lambda: 0.0),
        sleep=lambda _seconds: True,
    )

    entries, _source = strategy.fetch_entries(SEARCH_URL)

    assert strategy.get_initialization_entry_ids(entries) == {"42"}


def test_pazar3_strategy_rejects_pagination_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pages = iter(
        (
            Pazar3Page(
                (listing("1", minutes=1),),
                frozenset({EntryId("1")}),
                1,
                2,
                False,
            ),
            Pazar3Page(
                (listing("1", minutes=1),),
                frozenset({EntryId("1")}),
                1,
                2,
                True,
            ),
        ),
    )
    monkeypatch.setattr(pazar3, "fetch_pazar3_page", lambda *_args: b"page")
    monkeypatch.setattr(pazar3, "parse_pazar3_page", lambda *_args: next(pages))
    strategy = pazar3.Pazar3Strategy(
        clock=lambda: FIXED_NOW,
        pacer=Pazar3RequestPacer(lambda: 0.0),
        sleep=lambda _seconds: True,
    )

    with pytest.raises(FeedFetchError) as fetch_error:
        strategy.fetch_entries(SEARCH_URL)

    assert fetch_error.value.cause_type == "PaginationCycle"


def test_pazar3_strategy_preserves_scan_order_for_equal_timestamps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Pazar3Page(
        (listing("1", minutes=1), listing("2", minutes=1)),
        frozenset({EntryId("1"), EntryId("2")}),
        2,
        2,
        True,
    )
    monkeypatch.setattr(pazar3, "fetch_pazar3_page", lambda *_args: b"page")
    monkeypatch.setattr(pazar3, "parse_pazar3_page", lambda *_args: page)
    strategy = pazar3.Pazar3Strategy(
        clock=lambda: FIXED_NOW,
        pacer=Pazar3RequestPacer(lambda: 0.0),
        sleep=lambda _seconds: True,
    )

    entries, _source = strategy.fetch_entries(SEARCH_URL)

    assert [entry.entry_id for entry in entries] == ["1", "2"]
    assert strategy.max_delivery_history is None
