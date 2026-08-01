from __future__ import annotations

from datetime import datetime

import pytest

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports import FeedFetchError, reklama5_http
from rss2discord.transports import reklama5 as reklama5_transport
from tests.reklama5_helpers import (
    FIXED_NOW,
    SEARCH_URL,
    RecordingGet,
    Reklama5Card,
    StubResponse,
    requested_pages,
    search_page,
)


def _response(
    page: int,
    cards: list[Reklama5Card],
    *,
    page_links: list[int],
    result_count: int | None = None,
) -> StubResponse:
    return StubResponse(
        search_page(
            page,
            [card.html() for card in cards],
            page_links=page_links,
            result_count=len(cards) if result_count is None else result_count,
            active_page=page if page_links else None,
        ),
    )


def _strategy() -> reklama5_transport.Reklama5Strategy:
    return reklama5_transport.Reklama5Strategy(clock=lambda: FIXED_NOW)


def test_reklama5_strategy_fetches_at_most_three_pages_and_returns_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="300", timestamp="Денес 11:00")], page_links=[1, 2, 3]),
            _response(2, [Reklama5Card(ad_id="200", timestamp="Денес 10:00")], page_links=[1, 2, 3]),
            _response(3, [Reklama5Card(ad_id="100", timestamp="Денес 09:00")], page_links=[1, 2, 3]),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, source_title = _strategy().fetch_entries(SEARCH_URL)

    assert source_title == "Reklama5"
    assert requested_pages(get.urls) == ["1", "2", "3"]
    assert [entry.entry_id for entry in entries] == ["100", "200", "300"]


def test_reklama5_strategy_stops_before_next_page_when_shutdown_is_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="2")], page_links=[1, 2]),
            _response(2, [Reklama5Card(ad_id="1")], page_links=[1, 2]),
        ],
    )
    shutdown_checks = iter((False, True))
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)
    strategy = reklama5_transport.Reklama5Strategy(
        is_shutdown_requested=lambda: next(shutdown_checks),
        clock=lambda: FIXED_NOW,
    )

    with pytest.raises(FeedFetchInterruptedError):
        strategy.fetch_entries(SEARCH_URL)

    assert requested_pages(get.urls) == ["1"]


def test_reklama5_strategy_deduplicates_with_first_recent_occurrence_winning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(
                1,
                [Reklama5Card(ad_id="123", title="page-1-title")],
                page_links=[1, 2],
            ),
            _response(
                2,
                [
                    Reklama5Card(ad_id="123", title="page-2-title"),
                    Reklama5Card(ad_id="100"),
                ],
                page_links=[1, 2],
            ),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, _ = _strategy().fetch_entries(SEARCH_URL)

    duplicate = next(entry for entry in entries if entry.entry_id == "123")
    assert duplicate.title == "page-1-title"


def test_reklama5_strategy_stops_after_explicit_zero_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([_response(1, [], page_links=[], result_count=0)])
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, _ = _strategy().fetch_entries(SEARCH_URL)

    assert entries == []
    assert requested_pages(get.urls) == ["1"]


def test_reklama5_strategy_stops_after_non_empty_terminal_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([_response(1, [Reklama5Card(ad_id="1")], page_links=[])])
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, _ = _strategy().fetch_entries(SEARCH_URL)

    assert [entry.entry_id for entry in entries] == ["1"]
    assert requested_pages(get.urls) == ["1"]


def test_reklama5_strategy_continues_when_row_count_alone_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(
                1,
                [Reklama5Card(ad_id="2", title="")],
                page_links=[1, 2],
            ),
            _response(2, [Reklama5Card(ad_id="1")], page_links=[1, 2]),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, _ = _strategy().fetch_entries(SEARCH_URL)

    assert [entry.entry_id for entry in entries] == ["1"]
    assert requested_pages(get.urls) == ["1", "2"]


def test_reklama5_strategy_rejects_empty_non_terminal_page_before_page_three(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="2")], page_links=[1, 2, 3]),
            _response(2, [], page_links=[1, 2, 3], result_count=1),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    with pytest.raises(FeedFetchError) as fetch_error:
        _strategy().fetch_entries(SEARCH_URL)

    assert fetch_error.value.cause_type == "EmptyNonTerminalPage"
    assert not fetch_error.value.retryable


def test_reklama5_strategy_rejects_pagination_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="1")], page_links=[1, 2, 3]),
            _response(
                2,
                [Reklama5Card(ad_id="1")],
                page_links=[1, 2, 3],
            ),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    with pytest.raises(FeedFetchError) as fetch_error:
        _strategy().fetch_entries(SEARCH_URL)

    assert fetch_error.value.cause_type == "PaginationCycle"
    assert not fetch_error.value.retryable


def test_reklama5_strategy_rejects_duplicate_only_terminal_later_page_before_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="1")], page_links=[1, 2]),
            _response(2, [Reklama5Card(ad_id="1")], page_links=[1, 2]),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    with pytest.raises(FeedFetchError) as fetch_error:
        _strategy().fetch_entries(SEARCH_URL)

    assert fetch_error.value.cause_type == "PaginationCycle"
    assert not fetch_error.value.retryable


def test_reklama5_strategy_allows_later_page_with_at_least_one_new_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="2")], page_links=[1, 2]),
            _response(
                2,
                [Reklama5Card(ad_id="2"), Reklama5Card(ad_id="1")],
                page_links=[1, 2],
            ),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, _ = _strategy().fetch_entries(SEARCH_URL)

    assert {entry.entry_id for entry in entries} == {"1", "2"}


def test_reklama5_strategy_reverses_source_scan_position_for_equal_timestamp_ties(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            _response(
                1,
                [
                    Reklama5Card(ad_id="3"),
                    Reklama5Card(ad_id="2"),
                    Reklama5Card(ad_id="1"),
                ],
                page_links=[],
            ),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    entries, _ = _strategy().fetch_entries(SEARCH_URL)

    assert [entry.entry_id for entry in entries] == ["1", "2", "3"]


def test_reklama5_strategy_captures_one_aware_clock_value_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock_values: list[datetime] = []

    def clock() -> datetime:
        clock_values.append(FIXED_NOW)
        return FIXED_NOW

    get = RecordingGet(
        [
            _response(1, [Reklama5Card(ad_id="2")], page_links=[1, 2]),
            _response(2, [Reklama5Card(ad_id="1")], page_links=[1, 2]),
        ],
    )
    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)

    reklama5_transport.Reklama5Strategy(clock=clock).fetch_entries(SEARCH_URL)

    assert clock_values == [FIXED_NOW]
