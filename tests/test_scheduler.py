import pytest

from rss2discord.scheduler import (
    JobTiming,
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
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=run_ordinary),
            ),
            prices=(ScheduledJob("price", "price", interval=3600, run=run_price),),
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
    assert events[-2:] == [("price", 3600), ("ordinary", 3600)]
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
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=run_ordinary),
            ),
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
    assert clock.sleep_calls == [300, 200]


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
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=run_ordinary),
            ),
            prices=(ScheduledJob("price", "price", interval=3600, run=run_price),),
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
            ordinary=(
                ScheduledJob(
                    "ordinary",
                    "ordinary",
                    interval=300,
                    run=lambda: events.append(clock.now),
                ),
            ),
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
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=lambda: None),
            ),
            prices=(
                ScheduledJob(
                    "price",
                    "price",
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
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=fail_run),
            ),
            prices=(
                ScheduledJob(
                    "first",
                    "price",
                    interval=3600,
                    run=lambda: None,
                    close=fail_close,
                ),
                ScheduledJob(
                    "second",
                    "price",
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
    assert error.value.__notes__ == ["Job cleanup failed: first close failed"]


def test_scheduler_groups_multiple_cleanup_failures_without_primary_failure() -> None:
    clock = FakeSchedulerClock()
    clock.interrupt_next_sleep()

    def fail_first_close() -> None:
        raise ValueError("first close failed")

    def fail_second_close() -> None:
        raise TypeError("second close failed")

    scheduler = RuntimeScheduler(
        jobs=SchedulerJobs(
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=lambda: None),
            ),
            prices=(
                ScheduledJob(
                    "first",
                    "price",
                    interval=3600,
                    run=lambda: None,
                    close=fail_first_close,
                ),
                ScheduledJob(
                    "second",
                    "price",
                    interval=3600,
                    run=lambda: None,
                    close=fail_second_close,
                ),
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
            ordinary=(
                ScheduledJob("ordinary", "ordinary", interval=300, run=lambda: None),
            ),
            prices=(
                ScheduledJob(
                    "price",
                    "price",
                    interval=3600,
                    run=lambda: None,
                    close=fail_close,
                ),
            ),
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
        with pytest.raises(ExceptionGroup, match="Job cleanup failed"):
            scheduler.run()
        outer_notes = getattr(outer_error, "__notes__", None)

    assert outer_notes is None


def test_long_ordinary_job_does_not_starve_prices_or_next_feed() -> None:
    clock = FakeSchedulerClock()
    events: list[tuple[str, float]] = []
    timings: list[JobTiming] = []

    def slow() -> None:
        events.append(("slow", clock.now))
        clock.now += 1000

    RuntimeScheduler(
        SchedulerJobs(
            ordinary=(
                ScheduledJob("slow", "ordinary", 300, slow),
                ScheduledJob(
                    "next",
                    "ordinary",
                    300,
                    lambda: events.append(("next", clock.now)),
                ),
            ),
            prices=(
                ScheduledJob(
                    "price-a",
                    "price",
                    500,
                    lambda: events.append(("price-a", clock.now)),
                ),
                ScheduledJob(
                    "price-b",
                    "price",
                    500,
                    lambda: events.append(("price-b", clock.now)),
                ),
            ),
            ordinary_gap=61,
        ),
        SchedulerControl(clock.monotonic, clock.sleep, lambda: len(events) >= 4),
        observer=timings.append,
    ).run()

    assert events == [("slow", 0), ("price-a", 1000), ("price-b", 1000), ("next", 1061)]
    assert clock.sleep_calls == [61]
    assert timings[0] == JobTiming("slow", "ordinary", 0, 0, 1000)
    assert timings[-1] == JobTiming("next", "ordinary", 0, 1061, 1061)


def test_job_duration_keeps_original_phase_and_skips_missed_slots() -> None:
    clock = FakeSchedulerClock()
    starts: list[float] = []

    def run() -> None:
        starts.append(clock.now)
        clock.now += 25

    RuntimeScheduler(
        SchedulerJobs((ScheduledJob("a", "ordinary", 10, run),), ()),
        SchedulerControl(clock.monotonic, clock.sleep, lambda: len(starts) == 3),
    ).run()
    assert starts == [0, 30, 60]
    assert clock.sleep_calls == [5, 5]


def test_extreme_overrun_skips_arithmetically_and_preserves_due_peer() -> None:
    clock = FakeSchedulerClock()
    starts: list[float] = []

    def overrun() -> None:
        starts.append(clock.now)
        clock.now += 1_000_000_000_000

    RuntimeScheduler(
        SchedulerJobs(
            (ScheduledJob("slow", "ordinary", 1, overrun),),
            (ScheduledJob("peer", "price", 1, lambda: starts.append(clock.now)),),
        ),
        SchedulerControl(clock.monotonic, clock.sleep, lambda: len(starts) == 2),
    ).run()
    assert starts == [0, 1_000_000_000_000]
    assert clock.sleep_calls == []


def test_empty_scheduler_finishes_without_sleeping() -> None:
    clock = FakeSchedulerClock()
    RuntimeScheduler(
        SchedulerJobs((), ()),
        SchedulerControl(clock.monotonic, clock.sleep, lambda: False),
    ).run()
    assert clock.sleep_calls == []


def test_failed_job_observation_cannot_mask_primary_exception() -> None:
    clock = FakeSchedulerClock()

    def fail_job() -> None:
        raise ValueError("primary")

    def fail_observer(timing: JobTiming) -> None:
        assert timing.name == "a"
        raise RuntimeError("observer")

    with pytest.raises(ValueError, match="primary") as caught:
        RuntimeScheduler(
            SchedulerJobs((ScheduledJob("a", "ordinary", 1, fail_job),), ()),
            SchedulerControl(clock.monotonic, clock.sleep, lambda: False),
            observer=fail_observer,
        ).run()
    assert caught.value.__notes__ == ["Job timing observer failed: RuntimeError"]
