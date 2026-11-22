"""Immutable request and response limits for Hetzner Cloud availability."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

HETZNER_CLOUD_LABEL: Final = "Hetzner Cloud"
HETZNER_CLOUD_HOST: Final = "api.hetzner.cloud"
HETZNER_CLOUD_SERVER_TYPES_PATH: Final = "/v1/server_types"
HETZNER_CLOUD_URL: Final = (
    f"https://{HETZNER_CLOUD_HOST}{HETZNER_CLOUD_SERVER_TYPES_PATH}"
)
HETZNER_CLOUD_CONSOLE_URL: Final = "https://console.hetzner.cloud/"
HETZNER_CLOUD_TOKEN_ENV: Final = "HETZNER_CLOUD_TOKEN"  # noqa: S105
HETZNER_CLOUD_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
HETZNER_CLOUD_REQUEST_SECONDS: Final = 30
MAX_HETZNER_CLOUD_RESPONSE_BYTES: Final = 4 * 1024 * 1024
HETZNER_CLOUD_STREAM_CHUNK_BYTES: Final = 65_536
HETZNER_CLOUD_CURRENCY: Final = "EUR"
MAX_HETZNER_CLOUD_SNAPSHOTS: Final = 1_000
HETZNER_CLOUD_LOCATION_LABELS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "ash": "Ashburn",
        "fsn1": "Falkenstein",
        "hel1": "Helsinki",
        "hil": "Hillsboro",
        "nbg1": "Nuremberg",
        "sin": "Singapore",
    },
)
