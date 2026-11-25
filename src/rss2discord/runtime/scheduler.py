from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from math import floor, fmod, inf, isfinite, nextafter
from types import MappingProxyType
from typing import Literal


class JobOutcome(StrEnum):
    """Fetch outcome; legacy callbacks returning None count as attempted."""

    ATTEMPTED = "attempted"
    SKIPPED = "skipped"


type JobAction = Callable[[], JobOutcome | None]
type MonotonicClock = Callable[[], float]
type InterruptibleSleeper = Callable[[float], bool]
type ShutdownRequested = Callable[[], bool]


def _next_deadline(deadline: float, finished_at: float, interval: float) -> float:
    elapsed = max(0.0, finished_at - deadline)
    slots = elapsed / interval
    if isfinite(slots):
        following = deadline + (floor(slots) + 1) * interval
    else:
        # A finite elapsed time divided by a tiny interval can overflow.
        # Recover the phase remainder without constructing the slot count.
        following = finished_at + (interval - fmod(elapsed, interval))
    # Sub-ULP intervals (or rounded large slot counts) can leave the deadline
    # at completion. Skip to the next representable instant, never catch up
    # repeatedly at the same timestamp.
    return max(following, nextafter(finished_at, inf))


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    name: str
    kind: Literal["ordinary", "price"]
    interval: float
    run: JobAction
    close: Callable[[], None] | None = None
    host: str = ""

    def __post_init__(self) -> None:
        if not isfinite(self.interval) or self.interval <= 0:
            raise ValueError("Job interval must be positive and finite")


@dataclass(frozen=True, slots=True)
class SchedulerJobs:
    ordinary: tuple[ScheduledJob, ...]
    prices: tuple[ScheduledJob, ...]
    ordinary_gap: float = 0
    host_gaps: Mapping[str, float] = MappingProxyType({})
    default_host_gap: float = 0
    stagger_initial: bool = False

    def __post_init__(self) -> None:
        if not isfinite(self.ordinary_gap) or self.ordinary_gap < 0:
            raise ValueError("Ordinary gap must be nonnegative and finite")
        if not isfinite(self.default_host_gap) or self.default_host_gap < 0:
            raise ValueError("Default host gap must be nonnegative and finite")
        for host, gap in self.host_gaps.items():
            if not host:
                raise ValueError("Host gap keys must be non-empty")
            if not isfinite(gap) or gap < 0:
                raise ValueError("Host gaps must be nonnegative and finite")


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
        deadlines = self._initial_deadlines(jobs, started_at)
        ordinary_ready = started_at
        host_ready: dict[str, float] = {}
        cursor = 0
        last_kind: Literal["ordinary", "price"] = "price"

        primary_error: BaseException | None = None
        try:
            while jobs and not self._control.is_shutdown_requested():
                now = self._control.monotonic()
                eligible = [
                    self._eligible_time(
                        job,
                        deadline,
                        started_at=started_at,
                        ordinary_ready=ordinary_ready,
                        host_ready=host_ready,
                    )
                    for job, deadline in zip(jobs, deadlines, strict=True)
                ]
                index = self._select_job(
                    jobs,
                    deadlines,
                    eligible,
                    cursor,
                    now=now,
                    last_kind=last_kind,
                )
                if eligible[index] > now:
                    if not self._control.sleep(eligible[index] - now):
                        return
                    continue
                job = jobs[index]
                finished_at, outcome = self._execute(job, deadlines[index], now)
                # Fixed phase; skip missed slots arithmetically, without a
                # catch-up loop or completion-relative drift after long scans.
                deadlines[index] = _next_deadline(
                    deadlines[index],
                    finished_at,
                    job.interval,
                )
                if outcome != JobOutcome.SKIPPED:
                    if self._uses_host_pacing(job):
                        host_ready[job.host] = finished_at + self._host_gap(job.host)
                    elif job.kind == "ordinary":
                        ordinary_ready = finished_at + self._jobs.ordinary_gap
                cursor = (index + 1) % len(jobs)
                last_kind = job.kind
        except BaseException as error:
            primary_error = error
            raise
        finally:
            self._close_jobs(jobs, primary_error)

    def _initial_deadlines(
        self,
        jobs: tuple[ScheduledJob, ...],
        started_at: float,
    ) -> list[float]:
        if not self._jobs.stagger_initial:
            return [started_at for _ in jobs]
        deadlines = [started_at for _ in jobs]
        for kind in ("ordinary", "price"):
            indices = [index for index, job in enumerate(jobs) if job.kind == kind]
            if not indices:
                continue
            count = len(indices)
            for position, index in enumerate(indices):
                deadlines[index] = (
                    started_at + (position / count) * jobs[index].interval
                )
        return deadlines

    def _uses_host_pacing(self, job: ScheduledJob) -> bool:
        return bool(job.host) and (
            self._jobs.default_host_gap > 0 or job.host in self._jobs.host_gaps
        )

    def _host_gap(self, host: str) -> float:
        return self._jobs.host_gaps.get(host, self._jobs.default_host_gap)

    def _eligible_time(
        self,
        job: ScheduledJob,
        deadline: float,
        *,
        started_at: float,
        ordinary_ready: float,
        host_ready: Mapping[str, float],
    ) -> float:
        eligible = deadline
        if self._uses_host_pacing(job):
            eligible = max(eligible, host_ready.get(job.host, started_at))
        elif job.kind == "ordinary":
            eligible = max(eligible, ordinary_ready)
        return eligible

    @staticmethod
    def _select_job(
        jobs: tuple[ScheduledJob, ...],
        deadlines: list[float],
        eligible: list[float],
        cursor: int,
        *,
        now: float,
        last_kind: str,
    ) -> int:
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
        return index

    @staticmethod
    def _close_jobs(
        jobs: tuple[ScheduledJob, ...],
        primary_error: BaseException | None,
    ) -> None:
        cleanup_errors: list[Exception] = []
        for job in jobs:
            if job.close is not None:
                try:
                    job.close()
                except Exception as error:
                    cleanup_errors.append(error)
        if primary_error is not None:
            for cleanup_error in cleanup_errors:
                primary_error.add_note(f"Job cleanup failed: {cleanup_error}")
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
