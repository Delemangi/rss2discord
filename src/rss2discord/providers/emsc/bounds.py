"""Immutable request and response limits for EMSC earthquake scans."""

from typing import Final

EMSC_LABEL: Final = "EMSC"
EMSC_EVENT_URL: Final = "https://www.seismicportal.eu/eventdetails.html?unid={event_id}"
EMSC_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
EMSC_EVENT_TYPES: Final = frozenset({"ke", "se"})
MAX_EMSC_EVENTS: Final = 200
MAX_EMSC_RESPONSE_BYTES: Final = 1_048_576
EMSC_REQUEST_SECONDS: Final = 30
EMSC_STREAM_CHUNK_BYTES: Final = 65_536
