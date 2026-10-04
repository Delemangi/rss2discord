from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from math import floor, isfinite
from typing import Literal


class JobOutcome(StrEnum):
    """Fetch outcome; legacy callbacks returning None count as attempted."""

    ATTEMPTED = "attempted"
    SKIPPED = "skipped"


type JobAction = Callable[[], JobOutcome | None]
type MonotonicClock = Callable[[], float]
type InterruptibleSleeper = Callable[[float], bool]
type ShutdownRequested = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    name: str
    kind: Literal["ordinary", "price"]
    interval: float
    run: JobAction
    close: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        if not isfinite(self.interval) or self.interval <= 0:
            raise ValueError("Job interval must be positive and finite")


@dataclass(frozen=True, slots=True)
class SchedulerJobs:
    ordinary: tuple[ScheduledJob, ...]
    prices: tuple[ScheduledJob, ...]
    ordinary_gap: float = 0

    def __post_init__(self) -> None:
        if not isfinite(self.ordinary_gap) or self.ordinary_gap < 0:
            raise ValueError("Ordinary gap must be nonnegative and finite")


@dataclass(frozen=True, slots=True)
class JobTiming:
    name: str
    kind: Literal["ordinary", "price"]
    scheduled_at: float
    started_at: float
    finished_at: float


@dataclass(frozen=True, slots=True)
class SchedulerControl:
    monotonic: MonotonicClock
    sleep: InterruptibleSleeper
    is_shutdown_requested: ShutdownRequested


class RuntimeScheduler:
    """Runs ordinary and price jobs sequentially at monotonic deadlines."""

    def __init__(
        self,
        jobs: SchedulerJobs,
        control: SchedulerControl,
        observer: Callable[[JobTiming], None] | None = None,
    ) -> None:
        self._jobs = jobs
        self._control = control
        self._observer = observer

    def run(self) -> None:
        """Run due jobs until shutdown is requested or sleep is interrupted."""
        jobs = (*self._jobs.ordinary, *self._jobs.prices)
        started_at = self._control.monotonic()
        deadlines = [started_at for _ in jobs]
        ordinary_ready = started_at
        cursor = 0
        last_kind: Literal["ordinary", "price"] = "price"

        primary_error: BaseException | None = None
        try:
            while jobs and not self._control.is_shutdown_requested():
                now = self._control.monotonic()
                eligible = [
                    max(deadline, ordinary_ready)
                    if job.kind == "ordinary"
                    else deadline
                    for job, deadline in zip(jobs, deadlines, strict=True)
                ]
                index = min(
                    range(len(jobs)),
                    # The global gap can collapse distinct overdue ordinary
                    # deadlines. Preserve their age before rotating exact ties.
                    key=lambda index: (
                        eligible[index],
                        deadlines[index],
                        (index - cursor) % len(jobs),
                    ),
                )
                # Once both classes are ready, give each one service in turn.
                # This is equal-weight class fairness, not unconditional
                # ordinary priority (which would starve prices at gap zero).
                # Keep oldest-deadline ordering within a class, but do not let
                # a convoy of overdue prices delay an eligible ordinary job.
                due_other = [
                    candidate
                    for candidate, job in enumerate(jobs)
                    if eligible[candidate] <= now and job.kind != last_kind
                ]
                if due_other:
                    index = min(
                        due_other,
                        key=lambda candidate: (
                            deadlines[candidate],
                            (candidate - cursor) % len(jobs),
                        ),
                    )
                if eligible[index] > now:
                    if not self._control.sleep(eligible[index] - now):
                        return
                    continue
                job = jobs[index]
                finished_at, outcome = self._execute(job, deadlines[index], now)
                # Fixed phase; skip missed slots arithmetically, without a
                # catch-up loop or completion-relative drift after long scans.
                missed = max(0, floor((finished_at - deadlines[index]) / job.interval))
                deadlines[index] += (missed + 1) * job.interval
                if job.kind == "ordinary" and outcome != JobOutcome.SKIPPED:
                    ordinary_ready = finished_at + self._jobs.ordinary_gap
                cursor = (index + 1) % len(jobs)
                last_kind = job.kind
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_errors: list[Exception] = []
            for job in jobs:
                if job.close is not None:
                    try:
                        job.close()
                    except Exception as error:
                        cleanup_errors.append(error)
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"Job cleanup failed: {cleanup_error}",
                    )
            elif cleanup_errors:
                raise ExceptionGroup("Job cleanup failed", cleanup_errors)

    def _execute(
        self,
        job: ScheduledJob,
        scheduled_at: float,
        started_at: float,
    ) -> tuple[float, JobOutcome | None]:
        try:
            outcome = job.run()
        except BaseException as error:
            try:
                self._observe(job, scheduled_at, started_at, self._control.monotonic())
            except Exception as observation_error:
                error.add_note(
                    f"Job timing observer failed: {type(observation_error).__name__}",
                )
            raise
        finished_at = self._control.monotonic()
        if outcome != JobOutcome.SKIPPED:
            self._observe(job, scheduled_at, started_at, finished_at)
        return finished_at, outcome

    def _observe(
        self,
        job: ScheduledJob,
        scheduled_at: float,
        started_at: float,
        finished_at: float,
    ) -> None:
        if self._observer is not None:
            self._observer(
                JobTiming(job.name, job.kind, scheduled_at, started_at, finished_at),
            )
