from dataclasses import dataclass, replace
from typing import Any, assert_never

from app import RSSToDiscord
from configuration import AppConfig, FeedConfig
from delivery_store import DeliveryStore
from discord_client import SleepCallback, WebhookMessage
from models import EntryData, EntryId
from strategies import ScraperStrategy


@dataclass(frozen=True, slots=True)
class FakeEntry:
    id: EntryId
    data: EntryData


class FakeStrategy(ScraperStrategy):
    def __init__(self, entries: list[FakeEntry]) -> None:
        self.entries = entries

    def fetch_entries(self, url: str) -> tuple[list[Any], str]:
        return list(self.entries), "Source"

    def get_entry_id(self, entry: Any) -> EntryId | None:  # noqa: ANN401
        return entry.id

    def get_entry_data(self, entry: Any) -> EntryData:  # noqa: ANN401
        return entry.data


class FakeAdapter:
    def __init__(self) -> None:
        self.entries: list[FakeEntry] = []

    def adapt(self, entry: Any, data: EntryData) -> EntryData:  # noqa: ANN401
        self.entries.append(entry)
        return replace(data, author="Adapted Author")


class FakeSender:
    def __init__(self, outcomes: list[bool | RuntimeError]) -> None:
        self.outcomes = outcomes
        self.messages: list[WebhookMessage] = []

    def send(self, message: WebhookMessage, sleep: SleepCallback) -> bool:
        self.messages.append(message)
        outcome = self.outcomes.pop(0)
        match outcome:
            case RuntimeError():
                raise outcome
            case bool():
                return outcome
            case _ as unreachable:
                assert_never(unreachable)


def make_feed(feed_id: str) -> FeedConfig:
    return FeedConfig(
        id=feed_id,
        name=feed_id,
        url="https://example.test/feed.xml",
        webhook=f"https://discord.test/{feed_id}",
    )


def make_entry(entry_id: str) -> FakeEntry:
    return FakeEntry(
        id=EntryId(entry_id),
        data=EntryData(
            title=entry_id,
            link=f"https://example.test/{entry_id}",
            description="Description",
            author="Author",
            timestamp="2026-07-19T12:00:00+00:00",
        ),
    )


def make_app(
    store: DeliveryStore,
    sender: FakeSender,
    strategy: FakeStrategy,
    feeds: tuple[FeedConfig, ...],
) -> RSSToDiscord:
    app = RSSToDiscord(
        config=AppConfig(delay_between_posts=0, max_post_age_days=0, feeds=feeds),
        store=store,
        sender=sender,
    )
    app._strategies["rss"] = strategy
    return app
