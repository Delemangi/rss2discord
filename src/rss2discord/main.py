import logging
import os
import signal
import sqlite3
import sys
from pathlib import Path
from types import FrameType

import yaml
from pydantic import ValidationError

from .app import RSSToDiscord
from .configuration import AppConfig, FeedConfig, load_config
from .database_ownership import DatabaseOwnership, DatabaseOwnershipError
from .delivery_store import DeliveryStore
from .discord.client import DiscordWebhookClient
from .health_report import HealthSummaryReporter
from .state_maintenance import StateRetentionReporter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SAFE_VALIDATION_FIELDS = frozenset(AppConfig.model_fields) | frozenset(
    FeedConfig.model_fields,
)


def main() -> int:
    config_path = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))
    database_path = Path(os.environ.get("STATE_DB_PATH", "data/state.db"))

    try:
        config = load_config(config_path)
        with DatabaseOwnership(database_path), DeliveryStore(database_path) as store:
            removed_health = store.prune_unconfigured_health(
                feed.id for feed in config.feeds
            )
            if removed_health:
                logger.info(
                    "Removed %d health rows for unconfigured feeds",
                    removed_health,
                )
            _prune_state(store, config.state_retention_days)
            application = RSSToDiscord(
                config=config,
                store=store,
                sender=DiscordWebhookClient(),
            )
            _install_signal_handlers(application)
            retention_reporter = StateRetentionReporter(
                database_path=database_path,
                retention_days=config.state_retention_days,
            )
            retention_reporter.start()
            reporter: HealthSummaryReporter | None = None
            if config.health_webhook:
                reporter = HealthSummaryReporter(
                    webhook=config.health_webhook,
                    database_path=database_path,
                    interval=config.health_summary_interval,
                )
                reporter.start()
            try:
                application.run()
            finally:
                if reporter is not None:
                    reporter.stop()
                retention_reporter.stop()
    except DatabaseOwnershipError as error:
        logger.log(logging.ERROR, "Database writer startup refused: %s", error)
        return 1
    except FileNotFoundError:
        logger.log(logging.ERROR, "Configuration file not found: %s", config_path)
        return 1
    except ValidationError as error:
        logger.log(logging.ERROR, "Invalid startup data: %s", _format_error(error))
        return 1
    except yaml.YAMLError as error:
        logger.log(logging.ERROR, "Invalid YAML configuration%s", _yaml_location(error))
        return 1
    except sqlite3.Error as error:
        logger.log(
            logging.ERROR,
            "Storage initialization failed (%s: %s)",
            type(error).__name__,
            _sqlite_error_message(error),
        )
        return 1
    except OSError as error:
        logger.log(
            logging.ERROR,
            "Startup file operation failed (%s)",
            type(error).__name__,
        )
        return 1
    return 0


def _prune_state(store: DeliveryStore, retention_days: int) -> None:
    if retention_days <= 0:
        return
    try:
        result = store.prune_old_records(retention_days=retention_days)
        if result.total == 0:
            return
        logger.info(
            "Pruned state older than %d days: delivered_entries=%d "
            "price_notifications=%d price_history=%d price_snapshots=%d",
            retention_days,
            result.delivered_entries,
            result.price_notifications,
            result.price_history,
            result.price_snapshots,
        )
        store.vacuum()
    except sqlite3.Error as error:
        logger.log(
            logging.ERROR,
            "State retention failed (%s)",
            type(error).__name__,
        )


def _sqlite_error_message(error: sqlite3.Error) -> str:
    """Keep one bounded diagnostic line; SQLite messages carry no credentials."""
    message = " ".join(str(error).split())
    return message[:200] if message else "no detail"


def _format_error(error: ValidationError) -> str:
    messages = []
    for detail in error.errors():
        location = _format_location(detail["loc"])
        messages.append(f"{location}: {detail['msg']}")
    return "; ".join(messages)


def _format_location(location: tuple[str | int, ...]) -> str:
    safe_parts = []
    for part in location:
        if isinstance(part, int):
            safe_parts.append(str(part))
        elif part in SAFE_VALIDATION_FIELDS:
            safe_parts.append(part)
        else:
            safe_parts.append("<key>")
    return ".".join(safe_parts) or "configuration"


def _yaml_location(error: yaml.YAMLError) -> str:
    if not isinstance(error, yaml.MarkedYAMLError) or error.problem_mark is None:
        return ""
    return (
        f" at line {error.problem_mark.line + 1}, "
        f"column {error.problem_mark.column + 1}"
    )


def _install_signal_handlers(application: RSSToDiscord) -> None:
    def handle_shutdown(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        application.request_shutdown()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)


if __name__ == "__main__":
    raise SystemExit(main())
