import logging
import os
import signal
import sys
from pathlib import Path
from types import FrameType

import yaml
from pydantic import ValidationError

from app import RSSToDiscord
from configuration import load_config
from delivery_store import DeliveryStore, UnsupportedSchemaVersionError
from discord_client import DiscordWebhookClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def main() -> int:
    config_path = Path(os.environ.get("CONFIG_PATH", "config/config.yaml"))
    database_path = Path(os.environ.get("STATE_DB_PATH", "data/state.db"))
    legacy_state_path = Path(os.environ.get("LEGACY_STATE_PATH", "state.json"))

    try:
        config = load_config(config_path)
        with DeliveryStore(database_path, legacy_state_path, config.feeds) as store:
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
    except (OSError, UnsupportedSchemaVersionError):
        logger.exception("Unable to start RSS to Discord")
        return 1
    return 0


def _format_error(error: ValidationError) -> str:
    messages = []
    for detail in error.errors(
        include_context=False,
        include_input=False,
        include_url=False,
    ):
        location = ".".join(str(part) for part in detail["loc"]) or "configuration"
        messages.append(f"{location}: {detail['msg']}")
    return "; ".join(messages)


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
