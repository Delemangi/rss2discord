import logging
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import main as app_main
from configuration import load_config


def format_logs(caplog: pytest.LogCaptureFixture) -> str:
    formatter = logging.Formatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


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
    assert "secret-token" not in format_logs(caplog)


def test_malformed_yaml_does_not_expose_webhook_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "feeds:\n"
        "  - id: news\n"
        "    url: https://example.test/feed.xml\n"
        '    webhook: "secret-token\n',
    )
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    caplog.set_level(logging.ERROR)

    # When
    with pytest.raises(yaml.YAMLError):
        load_config(config_path)
    exit_code = app_main.main()

    # Then
    assert exit_code == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "secret-token" not in format_logs(caplog)


def test_database_open_failure_exits_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    config_path.write_text("feeds: []\n")
    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STATE_DB_PATH", str(tmp_path))
    caplog.set_level(logging.ERROR)

    # When
    exit_code = app_main.main()

    # Then
    logs = format_logs(caplog)
    assert exit_code == 1
    assert all(record.exc_info is None for record in caplog.records)
    assert "Storage initialization failed (OperationalError)" in logs
