import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

import main as app_main
from configuration import load_config


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


def test_invalid_config_does_not_expose_webhook_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    webhook_url = "https://discord.test/api/webhooks/id/secret-token"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/one.xml\n"
        f"    webhook: {webhook_url}\n"
        "  - id: news\n"
        "    url: https://example.test/two.xml\n"
        f"    webhook: {webhook_url}\n",
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    caplog.set_level(logging.ERROR)

    # When
    with pytest.raises(ValidationError) as validation_error:
        load_config(config_path)
    exit_code = app_main.main()

    # Then
    assert "secret-token" not in str(validation_error.value)
    assert exit_code == 1
    assert "secret-token" not in caplog.text
