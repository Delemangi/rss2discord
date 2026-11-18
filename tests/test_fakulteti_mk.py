import logging

import pytest

from rss2discord.models import SourceMetric
from rss2discord.providers.fakulteti_mk import catalog as fakulteti_mk_catalog
from rss2discord.providers.fakulteti_mk.bounds import (
    FAKULTETI_MK_ALL_CATEGORIES,
    MAX_FAKULTETI_MK_RESPONSE_BYTES,
)
from rss2discord.providers.fakulteti_mk.catalog import (
    FakultetiMkCatalogClient,
    normalize_fakulteti_mk_url,
)
from rss2discord.providers.fakulteti_mk.strategy import FakultetiMkStrategy
from rss2discord.transports import FeedFetchError
from tests.fakulteti_mk_helpers import (
    FAKULTETI_MK_SUBSET_URL,
    FAKULTETI_MK_URL,
    RecordingGet,
    StubResponse,
    json_payload,
    post,
)


def test_fakulteti_mk_subset_dedupes_and_sorts_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                json_payload(
                    [
                        post(
                            "a",
                            "A",
                            "18.11.2026",
                            description="A summary",
                            image="https://www.fakulteti.mk/a.jpg",
                            comments=3,
                            shares=4,
                        ),
                        post("b", "B", "17.11.2026"),
                    ],
                ),
            ),
            StubResponse(
                json_payload(
                    [
                        post("b", "B", "17.11.2026", category="nauka"),
                        post("c", "C", "16.11.2026", category="nauka"),
                    ],
                ),
            ),
            StubResponse(
                json_payload([post("d", "D", "15.11.2026", category="tehnologija")]),
            ),
            StubResponse(
                json_payload([post("e", "E", "14.11.2026", category="kariera")]),
            ),
        ],
    )
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)
    strategy = FakultetiMkStrategy()

    entries, source_title = strategy.fetch_entries(FAKULTETI_MK_SUBSET_URL)

    assert source_title == "Fakulteti.mk"
    assert strategy.seed_existing_on_first_fetch
    assert strategy.require_entries_for_initialization
    assert [entry.link.rsplit("/", 1)[-1] for entry in entries] == [
        "e",
        "d",
        "c",
        "b",
        "a",
    ]
    data = strategy.get_entry_data(entries[-1])
    assert data.title == "A"
    assert data.link == "https://www.fakulteti.mk/news/a"
    assert data.description == "A summary"
    assert data.image_url == "https://www.fakulteti.mk/a.jpg"
    assert data.timestamp == "2026-11-17T23:00:00+00:00"
    assert data.categories == ("Obrazovanie",)
    assert data.source_metrics == (
        SourceMetric(label="Comments", value="3"),
        SourceMetric(label="Shares", value="4"),
    )
    assert [call[1]["term"] for call in get.calls] == [
        "obrazovanie",
        "nauka",
        "tehnologija",
        "kariera",
    ]
    assert all(call[1]["command"] == "GetTaxonomyPosts" for call in get.calls)


def test_fakulteti_mk_root_fetches_latest_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [StubResponse(json_payload([post("a", "A", "18.11.2026")]))],
    )
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)

    entries, _source_title = FakultetiMkStrategy().fetch_entries(FAKULTETI_MK_URL)

    assert len(entries) == 1
    assert get.calls[0][1] == {
        "command": "GetLatestPosts",
        "offset": "0",
        "limit": "50",
    }


@pytest.mark.parametrize(
    "url",
    [
        "http://fakulteti.mk/",
        "https://evil.test/",
        "https://fakulteti.mk/category/unknown",
        "https://fakulteti.mk/category/obrazovanie,unknown",
        "https://fakulteti.mk/category/",
        "https://fakulteti.mk/?x=1",
        "https://fakulteti.mk/category/obrazovanie#fragment",
        "https://user:pass@fakulteti.mk/",
        "https://fakulteti.mk:8443/",
        "https://fakulteti.mk/category/"
        + ",".join([*FAKULTETI_MK_ALL_CATEGORIES, "extra"]),
    ],
)
def test_fakulteti_mk_rejects_invalid_urls(
    url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([])
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)

    with pytest.raises(FeedFetchError) as error:
        FakultetiMkCatalogClient().fetch_posts(url)

    assert error.value.cause_type == "InvalidUrl"
    assert get.calls == []


def test_fakulteti_mk_invalid_json_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    get = RecordingGet([StubResponse(b"not json")])
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)

    with pytest.raises(FeedFetchError) as error:
        FakultetiMkCatalogClient().fetch_posts(FAKULTETI_MK_URL)

    assert error.value.cause_type == "InvalidJson"


def test_fakulteti_mk_non_list_payload_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(b'{"latest":[]}')])
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)

    with pytest.raises(FeedFetchError) as error:
        FakultetiMkCatalogClient().fetch_posts(FAKULTETI_MK_URL)

    assert error.value.cause_type == "InvalidJson"


def test_fakulteti_mk_oversized_response_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [StubResponse(b"x" * (MAX_FAKULTETI_MK_RESPONSE_BYTES + 1))],
    )
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)

    with pytest.raises(FeedFetchError) as error:
        FakultetiMkCatalogClient().fetch_posts(FAKULTETI_MK_URL)

    assert error.value.cause_type == "ScanResponseTooLarge"


def test_fakulteti_mk_continues_after_category_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    get = RecordingGet(
        [
            StubResponse(b"", status_code=500),
            StubResponse(json_payload([post("b", "B", "17.11.2026")])),
        ],
    )
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)
    caplog.set_level(logging.WARNING)

    entries, _source_title = FakultetiMkStrategy().fetch_entries(
        "https://fakulteti.mk/category/obrazovanie,nauka",
    )

    assert [entry.link.rsplit("/", 1)[-1] for entry in entries] == ["b"]
    assert "category fetch failed: category=obrazovanie cause=HTTPError" in caplog.text


def test_fakulteti_mk_skips_unsafe_post_and_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                json_payload(
                    [
                        post(
                            "evil",
                            "Evil",
                            "18.11.2026",
                            url="https://evil.test/news/evil",
                        ),
                        post(
                            "http-image",
                            "HTTP image",
                            "17.11.2026",
                            image="http://www.fakulteti.mk/image.jpg",
                        ),
                        post(
                            "safe-image",
                            "Safe image",
                            "16.11.2026",
                            image="https://www.fakulteti.mk/image.jpg",
                        ),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)
    strategy = FakultetiMkStrategy()

    entries, _source_title = strategy.fetch_entries(FAKULTETI_MK_URL)

    assert [entry.link.rsplit("/", 1)[-1] for entry in entries] == [
        "safe-image",
        "http-image",
    ]
    assert strategy.get_entry_data(entries[0]).image_url == (
        "https://www.fakulteti.mk/image.jpg"
    )
    assert strategy.get_entry_data(entries[1]).image_url is None


def test_fakulteti_mk_keeps_post_with_invalid_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [StubResponse(json_payload([post("undated", "Undated", "not-a-date")]))],
    )
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)
    strategy = FakultetiMkStrategy()

    entries, _source_title = strategy.fetch_entries(FAKULTETI_MK_URL)

    assert len(entries) == 1
    assert entries[0].published_at is None
    assert strategy.get_entry_data(entries[0]).timestamp is None


def test_fakulteti_mk_empty_payload_returns_no_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet([StubResponse(json_payload([]))])
    monkeypatch.setattr(fakulteti_mk_catalog.requests, "get", get)

    assert FakultetiMkCatalogClient().fetch_posts(FAKULTETI_MK_URL) == ()


def test_normalize_fakulteti_mk_url_returns_ordered_unique_categories() -> None:
    assert normalize_fakulteti_mk_url(
        "https://www.fakulteti.mk/category/tehnologija,obrazovanie,tehnologija",
    ) == ("tehnologija", "obrazovanie")
