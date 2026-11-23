"""Shared test doubles for provider HTTP and Discord delivery."""

from __future__ import annotations

from collections.abc import Callable, Iterator, MutableMapping, Sequence
from dataclasses import dataclass, field

import requests

from rss2discord.discord.client import (
    DiscordDeliveryResult,
    SleepCallback,
    WebhookMessage,
)


@dataclass
class StubResponse:
    """Response surface shared by requests and curl-style provider fakes."""

    content: bytes = b""
    status_code: int = 200
    headers: MutableMapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] | None = None
    url: str = ""
    interruption: BaseException | None = None
    raw_header_lines: tuple[bytes, ...] | None = None
    on_chunk: Callable[[], None] | None = None
    text: str | None = None

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400 and "Location" in self.headers

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        if self.chunks is not None:
            yield from self.chunks
        elif self.text is not None:
            yield self.text.encode()
        else:
            yield self.content
        if self.interruption is not None:
            raise self.interruption

    def __enter__(self) -> StubResponse:
        return self

    def __exit__(self, *args: object) -> None:
        del args

    def close(self) -> None:
        return None


class RecordingSender:
    """Record outgoing messages and replay queued delivery outcomes."""

    def __init__(
        self,
        outcomes: Sequence[bool | DiscordDeliveryResult],
    ) -> None:
        self._outcomes = list(outcomes)
        self.messages: list[WebhookMessage] = []

    def send(
        self,
        message: WebhookMessage,
        sleep: SleepCallback,
    ) -> DiscordDeliveryResult:
        del sleep
        self.messages.append(message)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, bool):
            return (
                DiscordDeliveryResult.DELIVERED
                if outcome
                else DiscordDeliveryResult.FAILED
            )
        return outcome


class RetrySleepAdapter:
    """Adapt a retry callback to the sleep protocol used by provider tests."""

    def __init__(self, sleep: SleepCallback) -> None:
        self._sleep = sleep

    def __call__(self, seconds: float) -> bool:
        return self._sleep(seconds)
