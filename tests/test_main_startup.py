"""Startup, failure, and shutdown behavior of the process entrypoint."""

import logging
import signal
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from types import FrameType, SimpleNamespace

import pytest
import yaml

from rss2discord import main as main_module
from rss2discord.database_ownership import DatabaseOwnershipError
from rss2discord.main import main


class _Application:
    def __init__(self) -> None:
        self.ran = False
        self.shutdown_requested = False

    def run(self) -> None:
        self.ran = True

    def request_shutdown(self) -> None:
        self.shutdown_requested = True


def test_main_runs_application_with_environment_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    database_path = tmp_path / "state.db"
    config = SimpleNamespace(state_retention_days=0)
    store = object()
    sender = object()
    application = _Application()
    seen: dict[str, object] = {}

    def load(path: Path) -> object:
        seen["config_path"] = path
        return config

    def own(path: Path) -> object:
        seen["database_path"] = path
        return nullcontext()

    def deliver(path: Path) -> object:
        seen["store_path"] = path
        return nullcontext(store)

    def build_application(**kwargs: object) -> _Application:
        seen["application_kwargs"] = kwargs
        return application

    monkeypatch.setenv("CONFIG_PATH", str(config_path))
    monkeypatch.setenv("STATE_DB_PATH", str(database_path))
    monkeypatch.setattr(main_module, "load_config", load)
    monkeypatch.setattr(main_module, "DatabaseOwnership", own)
    monkeypatch.setattr(main_module, "DeliveryStore", deliver)
    monkeypatch.setattr(main_module, "DiscordWebhookClient", lambda: sender)
    monkeypatch.setattr(main_module, "RSSToDiscord", build_application)
    monkeypatch.setattr(
        main_module,
        "_install_signal_handlers",
        lambda app: seen.setdefault("installed", app),
    )

    assert main() == 0
    assert application.ran is True
    assert seen["config_path"] == config_path
    assert seen["database_path"] == database_path
    assert seen["store_path"] == database_path
    assert seen["installed"] is application
    kwargs = seen["application_kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["config"] is config
    assert kwargs["store"] is store
    assert kwargs["sender"] is sender


def test_main_returns_one_when_database_is_owned(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def refuse(path: Path) -> object:
        del path
        raise DatabaseOwnershipError("already owned")

    monkeypatch.setattr(main_module, "load_config", lambda path: object())
    monkeypatch.setattr(main_module, "DatabaseOwnership", refuse)

    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert "Database writer startup refused" in caplog.text


def test_main_returns_one_when_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def missing(path: Path) -> object:
        del path
        raise FileNotFoundError

    monkeypatch.setattr(main_module, "load_config", missing)

    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert "Configuration file not found" in caplog.text


def test_main_returns_one_on_file_operation_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail(path: Path) -> object:
        del path
        raise OSError("disk full")

    monkeypatch.setattr(main_module, "load_config", fail)

    with caplog.at_level(logging.ERROR):
        assert main() == 1
    assert "Startup file operation failed (OSError)" in caplog.text


def test_yaml_location_is_empty_without_a_problem_mark() -> None:
    assert main_module._yaml_location(yaml.YAMLError("broken")) == ""


def test_signal_handlers_request_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    handlers: dict[int, Callable[[int, FrameType | None], None]] = {}

    def register(
        signum: int,
        handler: Callable[[int, FrameType | None], None],
    ) -> None:
        handlers[signum] = handler

    monkeypatch.setattr(main_module.signal, "signal", register)
    application = _Application()

    main_module._install_signal_handlers(application)  # type: ignore[arg-type]

    assert set(handlers) == {signal.SIGTERM, signal.SIGINT}
    handlers[signal.SIGTERM](signal.SIGTERM, None)
    assert application.shutdown_requested is True
