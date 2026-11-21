"""Immutable request and response limits for the Arctic Shift archive."""

from typing import Final

ARCTIC_SHIFT_LABEL: Final = "Arctic Shift"
ARCTIC_SHIFT_ORIGIN: Final = "https://arctic-shift.photon-reddit.com"
ARCTIC_SHIFT_SEARCH_PATH: Final = "/api/posts/search"
ARCTIC_SHIFT_PAGE_SIZE: Final = 100
ARCTIC_SHIFT_REQUEST_SECONDS: Final = 30.0
MAX_ARCTIC_SHIFT_RESPONSE_BYTES: Final = 8 * 1024 * 1024
ARCTIC_SHIFT_STREAM_CHUNK_BYTES: Final = 64 * 1024
ARCTIC_SHIFT_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
