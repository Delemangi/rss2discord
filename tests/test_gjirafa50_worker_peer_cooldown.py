from collections.abc import Callable
from pathlib import Path
from threading import Event

import pytest

from rss2discord.configuration import AppConfig, FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_runtime import PriceJobDependencies, build_price_jobs
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.gjirafa50_background_monitor import (
    Gjirafa50BackgroundPriceMonitor,
)
from rss2discord.transports.gjirafa50_catalog import Gjirafa50CatalogClient
from rss2discord.transports.gjirafa50_models import Gjirafa50Product
from rss2discord.transports.gjirafa50_price_monitor import (
    Gjirafa50PriceMonitorDependencies,
)
from tests.app_helpers import FakeSender
from tests.test_gjirafa50_wiring import make_feed


def test_queued_same_origin_worker_rechecks_peer_cooldown_before_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_started = Event()
    release_first = Event()
    requested: list[str] = []
    workers: list[Gjirafa50BackgroundPriceMonitor] = []
    captured: list[tuple[str, ...]] = []
    first = make_feed(interval=21_600).model_copy(
        update={"id": "first", "url": "https://gjirafa50.mk/first"},
    )
    second = make_feed(interval=21_600).model_copy(
        update={"id": "second", "url": "https://gjirafa50.mk/second"},
    )
    ordinary_peer = make_feed().model_copy(update={"id": "ordinary-peer"})
    other_origin = make_feed().model_copy(
        update={"id": "other-origin", "url": "https://gjirafa50.com/"},
    )

    def fetch(
        self: Gjirafa50CatalogClient,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Gjirafa50Product, ...]:
        del self, retry_policy, is_shutdown_requested
        requested.append(url)
        if url == first.url:
            first_started.set()
            assert release_first.wait(3)
            raise FeedFetchError("Gjirafa50", "BotChallenge", status_code=403)
        return ()

    def factory(
        feed: FeedConfig,
        dependencies: Gjirafa50PriceMonitorDependencies,
    ) -> Gjirafa50BackgroundPriceMonitor:
        captured.append(dependencies.cooldown_peer_feed_ids)
        worker = Gjirafa50BackgroundPriceMonitor(feed, dependencies)
        workers.append(worker)
        return worker

    monkeypatch.setattr(Gjirafa50CatalogClient, "fetch_catalog", fetch)
    with DeliveryStore(tmp_path / "state.db") as store:
        jobs = build_price_jobs(
            AppConfig(feeds=(first, second, ordinary_peer, other_origin)),
            PriceJobDependencies(
                store=store,
                sender=FakeSender([]),
                sleep=lambda _: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
            gjirafa50_monitor_factory=factory,
        )
        assert len(jobs) == 2
        assert all(set(ids) == {"first", "second", "ordinary-peer"} for ids in captured)
        try:
            jobs[0].run()
            assert first_started.wait(2)
            # The runtime check passes: the first worker has not recorded its
            # challenge yet. The second is launched while the scan lock is held.
            assert store.get_blocked_until("first") is None
            jobs[1].run()
            assert workers[1]._thread is not None
            release_first.set()
            for worker in workers:
                assert worker._thread is not None
                worker._thread.join(4)
                assert not worker._thread.is_alive()
            assert requested == [first.url]
            assert store.get_blocked_until("first") is not None
            assert store.list_health("second") == ()
        finally:
            release_first.set()
            for worker in workers:
                worker.close()
