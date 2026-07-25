import importlib.metadata
import logging
import runpy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

import rss2discord.main as app_main
from rss2discord.configuration import load_config


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


def test_load_config_parses_setec_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: setec-new-products\n"
        "    url: https://setec.mk/e-prodazba\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: setec\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].strategy == "setec"


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


def test_module_entrypoint_delegates_to_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    calls: list[str] = []

    def fake_main() -> int:
        calls.append("main")
        return 7

    monkeypatch.setattr(app_main, "main", fake_main)

    # When
    with pytest.raises(SystemExit) as system_exit:
        runpy.run_module("rss2discord.__main__", run_name="__main__")

    # Then
    assert calls == ["main"]
    assert system_exit.value.code == 7


def test_console_script_targets_main() -> None:
    # Given / When
    entry_points = importlib.metadata.entry_points(group="console_scripts")
    entry_point = next(
        entry_point for entry_point in entry_points if entry_point.name == "rss2discord"
    )

    # Then
    assert entry_point.value == "rss2discord.main:main"
