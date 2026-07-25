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


def test_invalid_config_does_not_expose_webhook_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    webhook_url = "https://discord.test/api/webhooks/id/secret-token"
    config_path.write_text(
        "refresh_interval: 60\n"
        "delay_between_posts: 0\n"
        "max_post_age_days: 7\n"
        "feeds:\n"
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
