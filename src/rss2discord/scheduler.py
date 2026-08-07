from collections.abc import Callable
from dataclasses import dataclass

type JobAction = Callable[[], None]
type MonotonicClock = Callable[[], float]
type InterruptibleSleeper = Callable[[float], bool]
type ShutdownRequested = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    interval: float
    run: JobAction
    close: JobAction | None = None


@dataclass(frozen=True, slots=True)
class SchedulerJobs:
    ordinary: ScheduledJob
    prices: tuple[ScheduledJob, ...]


@dataclass(frozen=True, slots=True)
class SchedulerControl:
    monotonic: MonotonicClock
    sleep: InterruptibleSleeper
    is_shutdown_requested: ShutdownRequested


class RuntimeScheduler:
    """Runs ordinary and price jobs sequentially at monotonic deadlines."""

    def __init__(self, jobs: SchedulerJobs, control: SchedulerControl) -> None:
        self._jobs = jobs
        self._control = control

    def run(self) -> None:
        """Run due jobs until shutdown is requested or sleep is interrupted."""
        started_at = self._control.monotonic()
        ordinary_deadline = started_at
        price_deadlines = [started_at for _ in self._jobs.prices]

        primary_error: BaseException | None = None
        try:
            while not self._control.is_shutdown_requested():
                now = self._control.monotonic()
                if ordinary_deadline <= now:
                    self._jobs.ordinary.run()
                    ordinary_deadline = (
                        self._control.monotonic() + self._jobs.ordinary.interval
                    )
                    continue

                for price_index, price_deadline in enumerate(price_deadlines):
                    if price_deadline <= now:
                        price_job = self._jobs.prices[price_index]
                        price_job.run()
                        price_deadlines[price_index] = (
                            self._control.monotonic() + price_job.interval
                        )
                        break
                else:
                    next_deadline = min([ordinary_deadline, *price_deadlines])
                    if not self._control.sleep(max(0.0, next_deadline - now)):
                        return
        except BaseException as error:
            primary_error = error
            raise
        finally:
            cleanup_errors: list[Exception] = []
            for job in self._jobs.prices:
                if job.close is not None:
                    try:
                        job.close()
                    except Exception as error:
                        cleanup_errors.append(error)
            if primary_error is not None:
                for cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"Price job cleanup failed: {cleanup_error}",
                    )
            elif cleanup_errors:
                raise ExceptionGroup("Price job cleanup failed", cleanup_errors)
