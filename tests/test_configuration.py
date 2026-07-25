from pathlib import Path

import pytest
from pydantic import ValidationError

import rss2discord.main as app_main
from rss2discord.configuration import load_config


def write_config(path: Path, feeds: str) -> None:
    path.write_text(
        "refresh_interval: 60\n"
        "delay_between_posts: 0\n"
        "max_post_age_days: 7\n"
        f"feeds:\n{feeds}",
    )


def test_load_config_parses_valid_feed(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    name: News\n"
        "    url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: rss\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.refresh_interval == 60
    assert config.feeds[0].id == "news"
    assert config.feeds[0].strategy == "rss"


def test_load_config_disables_anhoch_price_check_interval_when_omitted(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval is None


def test_load_config_disables_anhoch_price_check_interval_when_null(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: null\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval is None


def test_load_config_parses_positive_anhoch_price_check_interval(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: 3600\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval == 3600


@pytest.mark.parametrize("interval", [0, -1])
def test_load_config_rejects_non_positive_anhoch_price_check_interval(
    tmp_path: Path,
    interval: int,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        f"    price_check_interval: {interval}\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_non_finite_anhoch_price_check_interval(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: .inf\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_price_check_interval_for_non_anhoch_feed(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: rss\n"
        "    price_check_interval: 3600\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_validation_location_allows_price_check_interval_name() -> None:
    # Given
    location = ("feeds", 0, "price_check_interval")

    # When
    formatted_location = app_main._format_location(location)

    # Then
    assert formatted_location == "feeds.0.price_check_interval"


def test_load_config_rejects_webhook_name_over_80_characters(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n"
        f"    webhook_name: {'n' * 81}\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_parses_delay_between_feeds(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    config_path.write_text("delay_between_feeds: 61\nfeeds: []\n")

    # When
    config = load_config(config_path)

    # Then
    assert config.delay_between_feeds == 61


def test_load_config_rejects_negative_delay_between_feeds(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    config_path.write_text("delay_between_feeds: -1\nfeeds: []\n")

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_missing_feed_id(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_duplicate_feed_ids(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/one.xml\n"
        "    webhook: https://discord.test/one\n"
        "  - id: news\n"
        "    url: https://example.test/two.xml\n"
        "    webhook: https://discord.test/two\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_unknown_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: typo\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_parses_hackernews_adapter(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: hacker-news\n"
        "    url: https://news.ycombinator.com/rss\n"
        "    webhook: https://discord.test/webhook\n"
        "    adapter: hackernews\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].adapter == "hackernews"


def test_load_config_rejects_unknown_adapter(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n"
        "    adapter: typo\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_adapter_with_xenforo_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: forum\n"
        "    url: https://forum.example.test/threads/topic.1/\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: xenforo\n"
        "    adapter: reddit\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_parses_itmk_oglasnik_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: itmk-oglasnik\n"
        "    url: https://forum.it.mk/oglasnik/\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: itmk_oglasnik\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].strategy == "itmk_oglasnik"


def test_load_config_rejects_adapter_with_itmk_oglasnik_strategy(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: itmk-oglasnik\n"
        "    url: https://forum.it.mk/oglasnik/\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: itmk_oglasnik\n"
        "    adapter: reddit\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_parses_anhoch_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch-new-products\n"
        "    url: https://www.anhoch.com/products?inStockOnly=2\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].strategy == "anhoch"
