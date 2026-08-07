import pytest

from rss2discord.scheduler import (
    RuntimeScheduler,
    ScheduledJob,
    SchedulerControl,
    SchedulerJobs,
)


class FakeSchedulerClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []
        self._sleep_result = True

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.sleep_calls.append(seconds)
        self.now += seconds
        return self._sleep_result

    def interrupt_next_sleep(self) -> None:
        self._sleep_result = False


def test_scheduler_runs_ordinary_and_price_jobs_immediately_and_by_deadline() -> None:
    # Given
    clock = FakeSchedulerClock()
    events: list[tuple[str, float]] = []

    def run_ordinary() -> None:
        events.append(("ordinary", clock.now))

    def run_price() -> None:
        events.append(("price", clock.now))

    def sleep_until_after_hour(seconds: float) -> bool:
        clock.sleep_calls.append(seconds)
        clock.now += seconds
        return clock.now < 3900

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=run_ordinary),
            prices=(ScheduledJob(interval=3600, run=run_price),),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=sleep_until_after_hour,
            is_shutdown_requested=lambda: False,
        ),
    )

    # When
    scheduler.run()

    # Then
    assert events[:4] == [
        ("ordinary", 0),
        ("price", 0),
        ("ordinary", 300),
        ("ordinary", 600),
    ]
    assert events[-2:] == [("ordinary", 3600), ("price", 3600)]
    assert clock.sleep_calls == [300] * 13


def test_scheduler_runs_once_when_a_sleep_overruns_a_job_deadline() -> None:
    # Given
    clock = FakeSchedulerClock()
    events: list[float] = []

    def run_ordinary() -> None:
        events.append(clock.now)

    def oversleep_once(seconds: float) -> bool:
        clock.sleep_calls.append(seconds)
        clock.now += 1000
        return len(clock.sleep_calls) == 1

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=run_ordinary),
            prices=(),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=oversleep_once,
            is_shutdown_requested=lambda: False,
        ),
    )

    # When
    scheduler.run()

    # Then
    assert events == [0, 1000]
    assert clock.sleep_calls == [300, 300]


def test_scheduler_runs_ordinary_immediately_after_price_job_overruns_deadline() -> (
    None
):
    # Given
    clock = FakeSchedulerClock()
    events: list[tuple[str, float]] = []

    def run_ordinary() -> None:
        events.append(("ordinary", clock.now))

    def run_price() -> None:
        events.append(("price", clock.now))
        clock.now += 1000

    def fail_on_sleep(seconds: float) -> bool:
        raise AssertionError(
            f"scheduler slept for {seconds} seconds instead of catching up",
        )

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=run_ordinary),
            prices=(ScheduledJob(interval=3600, run=run_price),),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=fail_on_sleep,
            is_shutdown_requested=lambda: len(events) >= 3,
        ),
    )

    # When
    scheduler.run()

    # Then
    assert events == [
        ("ordinary", 0),
        ("price", 0),
        ("ordinary", 1000),
    ]


def test_scheduler_stops_when_its_sleep_is_interrupted() -> None:
    # Given
    clock = FakeSchedulerClock()
    events: list[float] = []
    clock.interrupt_next_sleep()

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=lambda: events.append(clock.now)),
            prices=(),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            is_shutdown_requested=lambda: False,
        ),
    )

    # When
    scheduler.run()

    # Then
    assert events == [0]
    assert clock.sleep_calls == [300]


def test_scheduler_closes_price_jobs_after_interrupted_sleep() -> None:
    clock = FakeSchedulerClock()
    clock.interrupt_next_sleep()
    events: list[str] = []
    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=lambda: None),
            prices=(
                ScheduledJob(
                    interval=3600,
                    run=lambda: events.append("run"),
                    close=lambda: events.append("close"),
                ),
            ),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            is_shutdown_requested=lambda: False,
        ),
    )

    scheduler.run()

    assert events == ["run", "close"]


def test_scheduler_preserves_run_failure_and_attempts_every_close() -> None:
    clock = FakeSchedulerClock()
    closed: list[str] = []

    def fail_run() -> None:
        raise RuntimeError("run failed")

    def fail_close() -> None:
        closed.append("first")
        raise ValueError("first close failed")

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=fail_run),
            prices=(
                ScheduledJob(interval=3600, run=lambda: None, close=fail_close),
                ScheduledJob(
                    interval=3600,
                    run=lambda: None,
                    close=lambda: closed.append("second"),
                ),
            ),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            is_shutdown_requested=lambda: False,
        ),
    )

    with pytest.raises(RuntimeError, match="run failed") as error:
        scheduler.run()

    assert closed == ["first", "second"]
    assert error.value.__notes__ == ["Price job cleanup failed: first close failed"]


def test_scheduler_groups_multiple_cleanup_failures_without_primary_failure() -> None:
    clock = FakeSchedulerClock()
    clock.interrupt_next_sleep()

    def fail_first_close() -> None:
        raise ValueError("first close failed")

    def fail_second_close() -> None:
        raise TypeError("second close failed")

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=lambda: None),
            prices=(
                ScheduledJob(interval=3600, run=lambda: None, close=fail_first_close),
                ScheduledJob(interval=3600, run=lambda: None, close=fail_second_close),
            ),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            is_shutdown_requested=lambda: False,
        ),
    )

    with pytest.raises(ExceptionGroup) as error:
        scheduler.run()

    assert [str(failure) for failure in error.value.exceptions] == [
        "first close failed",
        "second close failed",
    ]


def test_scheduler_does_not_treat_outer_handled_exception_as_primary() -> None:
    clock = FakeSchedulerClock()
    clock.interrupt_next_sleep()

    def fail_close() -> None:
        raise RuntimeError("close failed")

    def fail_outer() -> None:
        raise ValueError("outer failure")

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=ScheduledJob(interval=300, run=lambda: None),
            prices=(ScheduledJob(interval=3600, run=lambda: None, close=fail_close),),
        ),
        control=SchedulerControl(
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            is_shutdown_requested=lambda: False,
        ),
    )

    outer_notes: list[str] | None = None
    try:
        fail_outer()
    except ValueError as outer_error:
        with pytest.raises(ExceptionGroup, match="Price job cleanup failed"):
            scheduler.run()
        outer_notes = getattr(outer_error, "__notes__", None)

    assert outer_notes is None
