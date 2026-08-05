import pytest

from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.pazar3_pacing import Pazar3RequestPacer


def test_pazar3_pacer_allows_first_request_then_waits_between_starts() -> None:
    now = [100.0]
    sleeps: list[float] = []
    pacer = Pazar3RequestPacer(lambda: now[0])

    def record_sleep(seconds: float) -> bool:
        sleeps.append(seconds)
        return True

    pacer.wait(record_sleep, lambda: False)
    now[0] = 105.0
    pacer.wait(record_sleep, lambda: False)

    assert sleeps == [15.0]


def test_pazar3_pacer_stops_before_first_request_when_shutdown_requested() -> None:
    pacer = Pazar3RequestPacer(lambda: 100.0)

    with pytest.raises(FeedFetchInterruptedError):
        pacer.wait(lambda _seconds: True, lambda: True)


def test_pazar3_pacer_rechecks_shutdown_after_no_wait_pacing() -> None:
    checks = iter((False, True))
    pacer = Pazar3RequestPacer(lambda: 100.0)

    with pytest.raises(FeedFetchInterruptedError):
        pacer.wait(lambda _seconds: True, lambda: next(checks))


def test_pazar3_pacer_stops_when_interruptible_wait_is_cancelled() -> None:
    pacer = Pazar3RequestPacer(lambda: 100.0)
    pacer.wait(lambda _seconds: True, lambda: False)

    with pytest.raises(FeedFetchInterruptedError):
        pacer.wait(lambda _seconds: False, lambda: False)
