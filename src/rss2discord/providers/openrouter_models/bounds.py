"""Immutable request and response limits for the OpenRouter model feed."""

from typing import Final

OPENROUTER_LABEL: Final = "OpenRouter"
OPENROUTER_HOST: Final = "openrouter.ai"
OPENROUTER_MODELS_PATH: Final = "/api/v1/models"
OPENROUTER_MODELS_URL: Final = f"https://{OPENROUTER_HOST}{OPENROUTER_MODELS_PATH}"
OPENROUTER_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
OPENROUTER_REQUEST_SECONDS: Final = 30
MAX_OPENROUTER_RESPONSE_BYTES: Final = 8 * 1024 * 1024
OPENROUTER_STREAM_CHUNK_BYTES: Final = 65_536
