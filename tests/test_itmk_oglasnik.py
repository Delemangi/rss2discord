import pytest
import requests

from rss2discord.configuration import FeedConfig
from rss2discord.discord.components import build_components_v2_payload
from rss2discord.models import SourceMetric
from rss2discord.transports import FeedFetchError, ITMkOglasnikStrategy
from tests.itmk_oglasnik_fixtures import (
    COMPLETE_CARD,
    INDEX_URL,
    NEWER_CARD,
    StubGet,
    make_response,
)


def test_itmk_oglasnik_strategy_extracts_rich_cards_oldest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = make_response(f"<h1>Нови огласи</h1>{NEWER_CARD}{COMPLETE_CARD}")
    monkeypatch.setattr(requests, "get", StubGet(response))
    strategy = ITMkOglasnikStrategy()

    # When
    entries, source_title = strategy.fetch_entries(INDEX_URL)
    entry = entries[0]
    data = strategy.get_entry_data(entry)

    # Then
    assert source_title == "Нови огласи"
    assert [strategy.get_entry_id(item) for item in entries] == ["6228", "6271"]
    assert data.title == "Iphone 12 64gb"
    assert data.link == "https://forum.it.mk/oglasnik/iphone-12-64gb.6228/"
    assert data.description == "Telefonot e vo odlicna sostojba."
    assert data.author == "Hristijan121"
    assert data.timestamp == "2026-07-10T09:36:09+02:00"
    assert data.image_url == "https://forum.it.mk/data/attachments/162/phone.jpg"
    assert data.categories == ("Мобилни уреди и додатоци", "Купено", "Заклучена")
    assert data.source_metrics == (
        SourceMetric(label="Price", value="8.000 ден."),
        SourceMetric(label="Condition", value="Користен одлично сочуван"),
        SourceMetric(label="Type", value="Продавам"),
        SourceMetric(label="Expires", value="2026-09-10T09:36:09+02:00"),
        SourceMetric(label="Views", value="2.817"),
    )


def test_itmk_oglasnik_strategy_sorts_promoted_cards_and_places_undated_last(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    promoted_old_card = """
    <div class="structItem">
      <div class="structItem-title"><a href="/oglasnik/old.9001/">Old</a></div>
      <div class="structItem-startDate"><time datetime="2026-07-06T08:00:00+0200"></time></div>
      <div class="structItem-listingDescription">Old summary</div>
    </div>
    """
    newer_card = """
    <div class="structItem">
      <div class="structItem-title"><a href="/oglasnik/new.9002/">New</a></div>
      <div class="structItem-startDate"><time datetime="2026-07-20T08:00:00+0200"></time></div>
      <div class="structItem-listingDescription">New summary</div>
    </div>
    """
    response = make_response(
        f"<h1>Огласник</h1>{NEWER_CARD}{promoted_old_card}{newer_card}",
    )
    monkeypatch.setattr(requests, "get", StubGet(response))
    strategy = ITMkOglasnikStrategy()

    # When
    entries, _ = strategy.fetch_entries(INDEX_URL)

    # Then
    assert [strategy.get_entry_id(entry) for entry in entries] == [
        "9001",
        "9002",
        "6271",
    ]


def test_itmk_oglasnik_strategy_skips_malformed_cards_and_placeholder_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    malformed_card = """
    <div class="structItem">
      <div class="structItem-title"><a href="/oglasnik/missing-id/">No ID</a></div>
      <div class="structItem-listingDescription"></div>
    </div>
    """
    response = make_response(f"<h1>Огласник</h1>{malformed_card}{NEWER_CARD}")
    monkeypatch.setattr(requests, "get", StubGet(response))
    strategy = ITMkOglasnikStrategy()

    # When
    entries, _ = strategy.fetch_entries(INDEX_URL)
    data = strategy.get_entry_data(entries[0])

    # Then
    assert len(entries) == 1
    assert data.image_url is None
    assert data.source_metrics == ()


def test_itmk_oglasnik_strategy_rejects_pages_without_valid_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = make_response("<html><h1>Огласник</h1></html>")
    monkeypatch.setattr(requests, "get", StubGet(response))
    strategy = ITMkOglasnikStrategy()

    # When / Then
    with pytest.raises(FeedFetchError, match="EmptyResponse"):
        strategy.fetch_entries(INDEX_URL)


def test_itmk_oglasnik_strategy_builds_rich_discord_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    response = make_response(f"<h1>Огласник</h1>{COMPLETE_CARD}")
    monkeypatch.setattr(requests, "get", StubGet(response))
    strategy = ITMkOglasnikStrategy()
    entries, source_title = strategy.fetch_entries(INDEX_URL)
    entry = strategy.get_entry_data(entries[0])
    feed = FeedConfig(
        id="itmk-oglasnik",
        url="https://forum.it.mk/oglasnik/",
        webhook="https://discord.test/webhook",
        strategy="itmk_oglasnik",
    )

    # When
    payload = build_components_v2_payload(feed, entry, source_title)

    # Then
    components = payload.get("components")
    assert isinstance(components, list)
    container = components[0]
    assert isinstance(container, dict)
    assert "8.000 ден." in str(container)
    assert "Мобилни уреди и додатоци" in str(container)
    assert "https://forum.it.mk/data/attachments/162/phone.jpg" in str(container)
