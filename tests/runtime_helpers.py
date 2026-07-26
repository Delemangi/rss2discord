class FakeClock:
    def __init__(self, maximum_sleeps: int) -> None:
        self.now = 0.0
        self.sleep_calls: list[float] = []
        self._maximum_sleeps = maximum_sleeps

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> bool:
        self.sleep_calls.append(seconds)
        self.now += seconds
        return len(self.sleep_calls) < self._maximum_sleeps


class RecordingMonitor:
    def __init__(
        self,
        feed_id: str,
        events: list[tuple[str, float]],
        clock: FakeClock,
    ) -> None:
        self._feed_id = feed_id
        self._events = events
        self._clock = clock

    def scan(self) -> None:
        self._events.append((self._feed_id, self._clock.now))
