import logging

import pytest

from rss2discord.providers.time_mk import catalog as time_mk_catalog
from rss2discord.providers.time_mk.bounds import TIME_MK_ALL_CATEGORIES
from rss2discord.providers.time_mk.catalog import TimeMkCatalogClient
from rss2discord.providers.time_mk.strategy import TimeMkStrategy
from rss2discord.transports import FeedFetchError
from tests.time_mk_helpers import (
    TIME_MK_CATEGORY_URL,
    TIME_MK_SUBSET_URL,
    TIME_MK_URL,
    DynamicGet,
    RecordingGet,
    StubResponse,
    article,
    details_payload,
    rss_payload,
)

TECH_CATEGORY = "Наука / Технологија"
MK_CATEGORY = "Македонија"
ECON_CATEGORY = "Економија"


def tech_article(
    suffix: str,
    title: str,
    source: str,
    published: str,
) -> str:
    return article(
        title,
        source,
        f"https://time.mk/c/{suffix}/mk/{title.lower().replace(' ', '-')}.html",
        published,
        TECH_CATEGORY,
    )


def test_time_mk_default_categories_dedupe_and_sort_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                rss_payload(
                    "tehnologija",
                    [
                        tech_article(
                            "aaaaaaaaaa",
                            "Tech A",
                            "IT.mk",
                            "Tue, 17 Nov 2026 15:00:00 +0000",
                        ),
                        tech_article(
                            "bbbbbbbbbb",
                            "Tech B",
                            "Trn",
                            "Tue, 17 Nov 2026 16:00:00 +0000",
                        ),
                    ],
                ),
            ),
            StubResponse(
                rss_payload(
                    "makedonija",
                    [
                        tech_article(
                            "aaaaaaaaaa",
                            "Tech A",
                            "IT.mk",
                            "Tue, 17 Nov 2026 15:00:00 +0000",
                        ),
                        article(
                            "MK C",
                            "Meta",
                            "https://time.mk/c/cccccccccc/mk/mk-c.html",
                            "Tue, 17 Nov 2026 14:00:00 +0000",
                            MK_CATEGORY,
                        ),
                    ],
                ),
            ),
            StubResponse(
                rss_payload(
                    "ekonomija",
                    [
                        article(
                            "Econ D",
                            "Faktor",
                            "https://time.mk/c/dddddddddd/mk/econ-d.html",
                            "Tue, 17 Nov 2026 17:00:00 +0000",
                            ECON_CATEGORY,
                        ),
                    ],
                ),
            ),
            StubResponse(
                details_payload(
                    "Tech A summary",
                    "https://cdn.example.test/tech-a.jpg",
                ),
            ),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)
    strategy = TimeMkStrategy()

    entries, source_title = strategy.fetch_entries(TIME_MK_URL)

    assert source_title == "Time.mk"
    assert strategy.seed_existing_on_first_fetch
    assert strategy.require_entries_for_initialization
    assert [strategy.get_entry_id(entry) for entry in entries] == [
        "cccccccccc",
        "aaaaaaaaaa",
        "bbbbbbbbbb",
        "dddddddddd",
    ]
    data = strategy.get_entry_data(entries[1])
    assert data.title == "Tech A"
    assert data.author == "IT.mk"
    assert data.link == "https://time.mk/c/aaaaaaaaaa/mk/tech-a.html"
    assert data.timestamp == "2026-11-17T15:00:00+00:00"
    assert data.categories == (TECH_CATEGORY,)
    assert data.description == "Tech A summary"
    assert data.image_url == "https://cdn.example.test/tech-a.jpg"
    assert [url.rsplit("/", 1)[-1] for url in get.urls[:3]] == [
        "tehnologija",
        "makedonija",
        "ekonomija",
    ]
    assert get.urls[3] == "https://time.mk/c/aaaaaaaaaa/mk/tech-a.html"


def test_time_mk_explicit_category_subset(monkeypatch: pytest.MonkeyPatch) -> None:
    get = RecordingGet(
        [
            StubResponse(
                rss_payload(
                    "tehnologija",
                    [
                        tech_article(
                            "aaaaaaaaaa",
                            "Tech A",
                            "IT.mk",
                            "Tue, 17 Nov 2026 15:00:00 +0000",
                        ),
                    ],
                ),
            ),
            StubResponse(
                rss_payload(
                    "ekonomija",
                    [
                        article(
                            "Econ D",
                            "Faktor",
                            "https://time.mk/c/dddddddddd/mk/econ-d.html",
                            "Tue, 17 Nov 2026 17:00:00 +0000",
                            ECON_CATEGORY,
                        ),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries(TIME_MK_SUBSET_URL)

    assert [entry.entry_id for entry in entries] == ["aaaaaaaaaa", "dddddddddd"]
    assert [url.rsplit("/", 1)[-1] for url in get.urls] == [
        "tehnologija",
        "ekonomija",
    ]


def test_time_mk_all_categories_fetches_every_category(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = {"value": 0}

    def build(category: str) -> str:
        counter["value"] += 1
        return article(
            f"Title {category}",
            "Source",
            f"https://time.mk/c/{counter['value']:010x}/mk/{category}.html",
            "Tue, 17 Nov 2026 15:00:00 +0000",
            category,
        )

    get = DynamicGet(build)
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries("https://time.mk/all")

    assert len(get.urls) == len(TIME_MK_ALL_CATEGORIES)
    assert len(entries) == len(TIME_MK_ALL_CATEGORIES)
    assert [url.rsplit("/", 1)[-1] for url in get.urls] == list(
        TIME_MK_ALL_CATEGORIES,
    )


def test_time_mk_continues_after_category_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING)
    get = RecordingGet(
        [
            StubResponse(b"error", status_code=500),
            StubResponse(
                rss_payload(
                    "makedonija",
                    [
                        article(
                            "MK C",
                            "Meta",
                            "https://time.mk/c/cccccccccc/mk/mk-c.html",
                            "Tue, 17 Nov 2026 14:00:00 +0000",
                            MK_CATEGORY,
                        ),
                    ],
                ),
            ),
            StubResponse(
                rss_payload(
                    "ekonomija",
                    [
                        article(
                            "Econ D",
                            "Faktor",
                            "https://time.mk/c/dddddddddd/mk/econ-d.html",
                            "Tue, 17 Nov 2026 17:00:00 +0000",
                            ECON_CATEGORY,
                        ),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries(TIME_MK_URL)

    assert [entry.entry_id for entry in entries] == ["cccccccccc", "dddddddddd"]
    assert "category fetch failed: category=tehnologija cause=HTTPError" in caplog.text


def test_time_mk_raises_when_every_category_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [StubResponse(b"error", status_code=500) for _ in range(3)],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    with pytest.raises(FeedFetchError, match="EmptyResponse"):
        TimeMkStrategy().fetch_entries(TIME_MK_URL)


def test_time_mk_empty_feeds_return_no_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(rss_payload(category, []))
            for category in ("tehnologija", "makedonija", "ekonomija")
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries(TIME_MK_URL)

    assert entries == []


def test_time_mk_skips_non_first_party_article_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                rss_payload(
                    "tehnologija",
                    [
                        article(
                            "Evil",
                            "Evil",
                            "https://evil.example/c/aaaaaaaaaa/mk/evil.html",
                            "Tue, 17 Nov 2026 15:00:00 +0000",
                            TECH_CATEGORY,
                        ),
                    ],
                ),
            ),
            StubResponse(rss_payload("makedonija", [])),
            StubResponse(rss_payload("ekonomija", [])),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries(TIME_MK_URL)

    assert entries == []


def test_time_mk_keeps_title_without_source_suffix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                rss_payload(
                    "tehnologija",
                    [
                        (
                            "<item><title>No Source</title>"
                            "<link>https://time.mk/c/aaaaaaaaaa/mk/no-source.html</link>"
                            "<pubDate>Tue, 17 Nov 2026 15:00:00 +0000</pubDate>"
                            f"<category>{TECH_CATEGORY}</category></item>"
                        ),
                    ],
                ),
            ),
            StubResponse(rss_payload("makedonija", [])),
            StubResponse(rss_payload("ekonomija", [])),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries(TIME_MK_URL)

    assert len(entries) == 1
    assert entries[0].title == "No Source"
    assert entries[0].source == ""


@pytest.mark.parametrize(
    "url",
    [
        "http://time.mk/",
        "https://user:pass@time.mk/",
        "https://example.com/",
        "https://time.mk/?query=1",
        "https://time.mk/news",
        "https://time.mk/rss/tehnologija#fragment",
    ],
)
def test_time_mk_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidUrl"):
        TimeMkStrategy().fetch_entries(url)


def test_time_mk_rejects_unknown_category() -> None:
    with pytest.raises(FeedFetchError, match="UnknownCategory"):
        TimeMkStrategy().fetch_entries("https://time.mk/rss/not-a-category")


def test_time_mk_single_category_url(monkeypatch: pytest.MonkeyPatch) -> None:
    get = RecordingGet(
        [
            StubResponse(
                rss_payload(
                    "tehnologija",
                    [
                        tech_article(
                            "aaaaaaaaaa",
                            "Tech A",
                            "IT.mk",
                            "Tue, 17 Nov 2026 15:00:00 +0000",
                        ),
                    ],
                ),
            ),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    entries, _source_title = TimeMkStrategy().fetch_entries(TIME_MK_CATEGORY_URL)

    assert len(entries) == 1
    assert len(get.urls) == 1


def test_time_mk_details_parse_open_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                details_payload(
                    "Article summary",
                    "https://cdn.example.test/article.jpg",
                ),
            ),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    details = TimeMkCatalogClient().fetch_details(
        "https://time.mk/c/aaaaaaaaaa/mk/tech-a.html",
    )

    assert details is not None
    assert details.description == "Article summary"
    assert details.image_url == "https://cdn.example.test/article.jpg"


def test_time_mk_details_reject_non_https_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                details_payload(
                    "Article summary",
                    "http://cdn.example.test/article.jpg",
                ),
            ),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)

    details = TimeMkCatalogClient().fetch_details(
        "https://time.mk/c/aaaaaaaaaa/mk/tech-a.html",
    )

    assert details is not None
    assert details.image_url is None


def test_time_mk_details_are_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = RecordingGet(
        [
            StubResponse(
                rss_payload(
                    "tehnologija",
                    [
                        tech_article(
                            "aaaaaaaaaa",
                            "Tech A",
                            "IT.mk",
                            "Tue, 17 Nov 2026 15:00:00 +0000",
                        ),
                    ],
                ),
            ),
            StubResponse(b"error", status_code=500),
        ],
    )
    monkeypatch.setattr(time_mk_catalog.requests, "get", get)
    strategy = TimeMkStrategy()

    entries, _source_title = strategy.fetch_entries(TIME_MK_CATEGORY_URL)
    data = strategy.get_entry_data(entries[0])

    assert data.description == ""
    assert data.image_url is None
