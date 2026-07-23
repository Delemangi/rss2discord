"""URL construction for the two Anhoch catalog scan modes."""

from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit, urlunsplit

from rss2discord.transports.anhoch_catalog_bounds import ANHOCH_LABEL
from rss2discord.transports.base import FeedFetchError


def catalog_base_url(url: str) -> str:
    """Return a full-catalog URL without caller-supplied query filters."""
    return urlunsplit(_parse_url(url)._replace(query=""))


def page_url(url: str, *, page_number: int, per_page: int) -> str:
    """Add Anhoch pagination while preserving ordinary feed filters."""
    parsed_url = _parse_url(url)
    query = [
        (key, value)
        for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
        if key not in {"sort", "perPage", "page"}
    ]
    query.extend(
        (("sort", "latest"), ("perPage", str(per_page)), ("page", str(page_number))),
    )
    return urlunsplit(parsed_url._replace(query=urlencode(query)))


def _parse_url(url: str) -> SplitResult:
    try:
        return urlsplit(url)
    except ValueError:
        raise FeedFetchError(ANHOCH_LABEL, "InvalidUrl") from None
