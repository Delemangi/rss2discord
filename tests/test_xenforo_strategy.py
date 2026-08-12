import traceback
from pathlib import Path

import pytest

import rss2discord.transports.xenforo as xenforo_module
from rss2discord.transports import FeedFetchError, XenForoStrategy
from tests.discord_components_helpers import get_text_display_contents, make_message


def test_xenforo_strategy_requires_post_id() -> None:
    # Given
    strategy = XenForoStrategy()

    # When / Then
    assert strategy.get_entry_id({"id": 42}) == "42"
    assert strategy.get_entry_id({"content": "No stable identity"}) is None


def test_xenforo_strategy_does_not_invent_missing_timestamp() -> None:
    # Given
    strategy = XenForoStrategy()

    # When / Then
    assert strategy._get_timestamp({}) is None


def test_xenforo_strategy_builds_post_permalink_from_latest_url() -> None:
    # Given
    strategy = XenForoStrategy()
    entry = {
        "id": 10481134,
        "thread_url": "https://forum.example.test/threads/topic.68732/latest",
    }

    # When
    entry_data = strategy.get_entry_data(entry)

    # Then
    assert entry_data.link == (
        "https://forum.example.test/threads/topic.68732/post-10481134"
    )


def test_xenforo_strategy_omits_credentials_from_post_permalink() -> None:
    # Given
    strategy = XenForoStrategy()
    entry = {
        "id": 7,
        "thread_url": "https://user:secret@forum.example.test/threads/topic.1/",
    }

    # When
    entry_data = strategy.get_entry_data(entry)

    # Then
    assert entry_data.link == ""


def test_xenforo_fetch_does_not_change_process_working_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    original_cwd = Path.cwd()
    scraper_working_directories: list[Path] = []

    class RecordingScraper:
        def get_thread(
            self,
            url: str,
        ) -> dict[str, dict[str, list[dict[str, str | list[str]]]]]:
            scraper_working_directories.append(Path.cwd())
            return {
                "data": {
                    "threads": [
                        {
                            "title": "Thread",
                            "url": url,
                            "posts": [],
                        },
                    ],
                },
            }

    monkeypatch.setattr(xenforo_module, "xenforo", lambda **kwargs: RecordingScraper())

    # When
    XenForoStrategy().fetch_entries("https://forum.example.test/threads/topic.1/")

    # Then
    assert scraper_working_directories == [original_cwd]


def test_xenforo_fetch_uses_configured_thread_url_when_scraper_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    configured_url = "https://forum.example.test/threads/topic.1/"

    class ThreadWithoutUrlScraper:
        def get_thread(
            self,
            url: str,
        ) -> dict[
            str,
            dict[str, list[dict[str, str | list[dict[str, int | str]]]]],
        ]:
            del url
            return {
                "data": {
                    "threads": [
                        {
                            "title": "Thread",
                            "posts": [{"id": 7, "content": "Post body"}],
                        },
                    ],
                },
            }

    monkeypatch.setattr(
        xenforo_module,
        "xenforo",
        lambda **kwargs: ThreadWithoutUrlScraper(),
    )
    strategy = XenForoStrategy()

    # When
    entries, _source_title = strategy.fetch_entries(configured_url)
    entry_data = strategy.get_entry_data(entries[0])
    heading = get_text_display_contents(
        make_message(strategy="xenforo", entry=entry_data),
    )[0]

    # Then
    assert entry_data.link == f"{configured_url.rstrip('/')}/post-7"
    assert entry_data.link in heading


def test_xenforo_fetch_removes_configured_url_secrets_from_permalink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    configured_url = (
        "https://forum.example.test/threads/topic.1/?token=secret-token#latest"
    )

    class ThreadWithoutUrlScraper:
        def get_thread(
            self,
            url: str,
        ) -> dict[
            str,
            dict[str, list[dict[str, str | list[dict[str, int | str]]]]],
        ]:
            del url
            return {
                "data": {
                    "threads": [
                        {
                            "title": "Thread",
                            "posts": [{"id": 7, "content": "Post body"}],
                        },
                    ],
                },
            }

    monkeypatch.setattr(
        xenforo_module,
        "xenforo",
        lambda **kwargs: ThreadWithoutUrlScraper(),
    )
    strategy = XenForoStrategy()

    # When
    entries, _source_title = strategy.fetch_entries(configured_url)
    entry_data = strategy.get_entry_data(entries[0])
    heading = get_text_display_contents(
        make_message(strategy="xenforo", entry=entry_data),
    )[0]

    # Then
    assert entry_data.link == "https://forum.example.test/threads/topic.1/post-7"
    assert "secret-token" not in heading


@pytest.mark.parametrize(
    "scraper_url",
    [
        "javascript:alert(1)",
        "https://forum.example.test:bad/threads/topic.1/",
        "https://forum.example.test:99999/threads/topic.1/",
    ],
)
def test_xenforo_fetch_falls_back_from_invalid_scraper_url(
    monkeypatch: pytest.MonkeyPatch,
    scraper_url: str,
) -> None:
    # Given
    configured_url = "https://forum.example.test/threads/topic.1/"

    class ThreadWithInvalidUrlScraper:
        def get_thread(
            self,
            url: str,
        ) -> dict[
            str,
            dict[str, list[dict[str, str | list[dict[str, int | str]]]]],
        ]:
            del url
            return {
                "data": {
                    "threads": [
                        {
                            "title": "Thread",
                            "url": scraper_url,
                            "posts": [{"id": 7, "content": "Post body"}],
                        },
                    ],
                },
            }

    monkeypatch.setattr(
        xenforo_module,
        "xenforo",
        lambda **kwargs: ThreadWithInvalidUrlScraper(),
    )
    strategy = XenForoStrategy()

    # When
    entries, _source_title = strategy.fetch_entries(configured_url)
    entry_data = strategy.get_entry_data(entries[0])

    # Then
    assert entry_data.link == "https://forum.example.test/threads/topic.1/post-7"


def test_xenforo_fetch_error_does_not_expose_feed_url_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    feed_url = "https://feed.test/thread?token=secret-token"

    class XenForoRequestError(Exception):
        pass

    class FailingScraper:
        def get_thread(self, url: str) -> None:
            raise XenForoRequestError(f"Could not connect to {url}")

    monkeypatch.setattr(xenforo_module, "xenforo", lambda **kwargs: FailingScraper())
    # When
    with pytest.raises(FeedFetchError) as fetch_error:
        XenForoStrategy().fetch_entries(feed_url)

    # Then
    rendered_error = "".join(
        traceback.format_exception(
            fetch_error.type,
            fetch_error.value,
            fetch_error.tb,
        ),
    )
    assert "secret-token" not in rendered_error
