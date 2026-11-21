"""Immutable request and response limits for the Hetzner auction feed."""

from typing import Final

HETZNER_AUCTION_LABEL: Final = "Hetzner Auction"
HETZNER_AUCTION_HOST: Final = "www.hetzner.com"
HETZNER_AUCTION_PATH: Final = "/_resources/app/data/app/live_data_sb.json"
HETZNER_AUCTION_URL: Final = f"https://{HETZNER_AUCTION_HOST}{HETZNER_AUCTION_PATH}"
HETZNER_USER_AGENT: Final = (
    "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
)
HETZNER_REQUEST_SECONDS: Final = 30
MAX_HETZNER_RESPONSE_BYTES: Final = 5 * 1024 * 1024
HETZNER_STREAM_CHUNK_BYTES: Final = 65_536
MAX_HETZNER_SERVERS: Final = 1_000
