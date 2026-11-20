"""Shared fixtures and fakes for Reddit Data API tests."""

import json
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
import requests

from rss2discord.providers.reddit.client import RedditClient
from rss2discord.providers.reddit.models import RedditPage
from rss2discord.providers.reddit.scope import RedditScope, parse_reddit_scope

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "reddit"
REDDIT_SCOPE = RedditScope(subreddit="python", sort="new")
DEFAULT_USER_AGENT = "rss2discord-test/1.0"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def fixture_json(name: str) -> dict[str, Any]:
    payload = json.loads(fixture_bytes(name))
    assert isinstance(payload, dict)
    return payload


def scope_for(url: str) -> RedditScope:
    return parse_reddit_scope(url)


class FakeResponse:
    """Minimal streamed response surface used by the bounded reader."""

    def __init__(
        self,
        status_code: int,
        content: bytes = b"",
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self._content = content
        self.headers = dict(headers or {})

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        if self._content:
            yield self._content

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        del args


class FakeSession:
    """Record requests and replay queued responses or exceptions."""

    def __init__(self, responses: Sequence[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(
        self,
        url: str,
        *,
        data: Mapping[str, str],
        auth: tuple[str, str],
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> FakeResponse:
        self.calls.append(
            (
                "POST",
                url,
                {
                    "data": dict(data),
                    "auth": auth,
                    "headers": dict(headers),
                    "timeout": timeout,
                    "stream": stream,
                },
            ),
        )
        return self._next()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
    ) -> FakeResponse:
        self.calls.append(
            (
                "GET",
                url,
                {
                    "params": dict(params),
                    "headers": dict(headers),
                    "timeout": timeout,
                    "stream": stream,
                },
            ),
        )
        return self._next()

    def _next(self) -> FakeResponse:
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class StepClock:
    """A monotonic clock advanced explicitly by tests."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeListingClient:
    """Return queued listing pages and record pagination arguments."""

    def __init__(self, pages: Sequence[RedditPage]) -> None:
        self.pages = list(pages)
        self.calls: list[tuple[RedditScope, str | None, int]] = []

    def fetch_listing(
        self,
        scope: RedditScope,
        *,
        after: str | None = None,
        count: int = 0,
    ) -> RedditPage:
        self.calls.append((scope, after, count))
        return self.pages.pop(0)


def set_reddit_environment(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user_agent: str = DEFAULT_USER_AGENT,
) -> None:
    monkeypatch.setenv("REDDIT_CLIENT_ID", "fixture-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "fixture-secret")
    monkeypatch.setenv("REDDIT_USER_AGENT", user_agent)
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)


def make_reddit_client(
    session: FakeSession,
    **kwargs: Any,  # noqa: ANN401
) -> RedditClient:
    return RedditClient(
        session=session,
        min_request_interval=0,
        **kwargs,
    )


def raised_connection_error() -> requests.ConnectionError:
    return requests.ConnectionError("connection reset")
