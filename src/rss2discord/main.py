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
from .configuration import load_config
from .delivery_store import DeliveryStore
from .discord.client import DiscordWebhookClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

SAFE_VALIDATION_FIELDS = frozenset(
    {
        "adapter",
        "delay_between_feeds",
        "delay_between_posts",
        "embed_color",
        "feeds",
        "id",
        "max_post_age_days",
        "name",
        "price_check_interval",
        "refresh_interval",
        "strategy",
        "url",
        "webhook",
        "webhook_avatar",
        "webhook_name",
    },
)


def main() -> int:
    config_path = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))
    database_path = Path(os.environ.get("STATE_DB_PATH", "data/state.db"))

    try:
        config = load_config(config_path)
        with DeliveryStore(database_path) as store:
            application = RSSToDiscord(
                config=config,
                store=store,
                sender=DiscordWebhookClient(),
            )
            _install_signal_handlers(application)
            application.run()
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
            "Storage initialization failed (%s)",
            type(error).__name__,
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
