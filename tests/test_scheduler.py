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
