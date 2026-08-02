from __future__ import annotations

from pathlib import Path

import pytest

from rss2discord.app import RSSToDiscord
from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import (
    DiscordDeliveryResult,
    SleepCallback,
    WebhookMessage,
)
from rss2discord.transports import reklama5 as reklama5_transport
from rss2discord.transports import reklama5_http
from rss2discord.transports.reklama5_http import (
    MAX_REKLAMA5_ATTEMPT_BYTES,
    MAX_REKLAMA5_ATTEMPT_SECONDS,
    Reklama5ScanBudget,
)
from tests.reklama5_helpers import (
    FIXED_NOW,
    SEARCH_URL,
    RecordingGet,
    Reklama5Card,
    StubResponse,
    requested_pages,
    search_page,
)


class UnusedSender:
    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del message, sleep
        raise AssertionError("sender should not be called")


def _page(page: int, ad_id: str, page_links: list[int]) -> bytes:
    return search_page(
        page,
        [Reklama5Card(ad_id=ad_id).html()],
        page_links=page_links,
        result_count=1,
        active_page=page,
    )


def test_reklama5_retry_restarts_with_page_one_and_a_fresh_attempt_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(_page(1, "2", [1, 2])),
            StubResponse(b"unavailable", status_code=503),
            StubResponse(_page(1, "2", [1, 2])),
            StubResponse(_page(2, "1", [1, 2])),
        ],
    )
    budgets: list[Reklama5ScanBudget] = []
    budget_starts: list[tuple[int, float]] = []
    original_for_attempt = Reklama5ScanBudget.for_attempt

    def record_budget() -> Reklama5ScanBudget:
        budget = original_for_attempt()
        budgets.append(budget)
        budget_starts.append((budget.bytes_remaining, budget.expires_at))
        return budget

    monkeypatch.setattr(reklama5_http, "_create_session", lambda: get)
    monkeypatch.setattr(Reklama5ScanBudget, "for_attempt", record_budget)
    monkeypatch.setattr(reklama5_http.time, "monotonic", lambda: 10.0)
    feed = FeedConfig(
        id="reklama5",
        name="Reklama5",
        url=SEARCH_URL,
        webhook="https://discord.test/webhook",
    )

    with DeliveryStore(tmp_path / "state.db") as store:
        app = RSSToDiscord(AppConfig(feeds=(feed,)), store, UnusedSender())
        monkeypatch.setattr(app, "_interruptible_sleep", lambda _seconds: True)

        entries, title = app._fetch_entries(
            feed,
            reklama5_transport.Reklama5Strategy(clock=lambda: FIXED_NOW),
        )

    assert title == "Reklama5"
    assert [entry.entry_id for entry in entries] == ["1", "2"]
    assert requested_pages(get.urls) == ["1", "2", "1", "2"]
    assert get.timeouts == [30.0, 30.0, 30.0, 30.0]
    assert len(budgets) == 2
    assert budget_starts == [
        (MAX_REKLAMA5_ATTEMPT_BYTES, 10.0 + MAX_REKLAMA5_ATTEMPT_SECONDS),
        (MAX_REKLAMA5_ATTEMPT_BYTES, 10.0 + MAX_REKLAMA5_ATTEMPT_SECONDS),
    ]
    assert all(
        budget.expires_at == 10.0 + MAX_REKLAMA5_ATTEMPT_SECONDS for budget in budgets
    )
    assert all(
        budget.bytes_remaining < MAX_REKLAMA5_ATTEMPT_BYTES for budget in budgets
    )
