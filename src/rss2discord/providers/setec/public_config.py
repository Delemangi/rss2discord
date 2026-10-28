"""Bounded, operation-local discovery; downloaded URLs never receive credentials."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from time import monotonic
from urllib.parse import urljoin, urlsplit, urlunsplit

from rss2discord.providers.setec.catalog_bounds import SETEC_LABEL
from rss2discord.retries import FeedFetchInterruptedError
from rss2discord.transports.base import FeedFetchError

SETEC_HOST = "setec.mk"
HOMEPAGE = f"https://{SETEC_HOST}/"
MAX_ASSETS = 40
MAX_DISCOVERY_REQUESTS = 48
MAX_DISCOVERY_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_BYTES = 16 * 1024 * 1024
DISCOVERY_SECONDS = 120.0
_CHUNK_PATH_PREFIX = "static/chunks/"
_STATIC_PATH_PREFIX = "/_next/static/"

# Direct constructor plus immediately associated index, not independent literals.
_CONSTRUCTOR = re.compile(
    r"""(?:let|const|var)\s+[\w$]+\s*=\s*\(0,[\w$]+\.[\w$]+\)\("""
    r"""\s*(["'])(https://search\.sp\.solslab\.dev/?)\1\s*,\s*"""
    r"""(["'])([A-Za-z0-9_-]{16,256})\3\s*\)\s*,\s*"""
    r"""[\w$]+\s*=\s*(["'])products\5""",
)
_QUOTED_PATH = re.compile(r"""(?=["']([^"'<>\s]++)["'])""")


def _advertised_paths(text: str) -> Iterator[str]:
    """Inspect bounded quoted tokens without repeatedly backtracking over URLs."""
    accepted_end = 0
    for match in _QUOTED_PATH.finditer(text):
        if match.start() < accepted_end:
            continue
        value = match[1]
        suffix = _static_script_suffix(value)
        if suffix is not None and _has_script_suffix(suffix):
            accepted_end = match.start() + len(value) + 2
            yield value


def _static_script_suffix(value: str) -> str | None:
    if value.startswith(_CHUNK_PATH_PREFIX):
        return value[len(_CHUNK_PATH_PREFIX) :]
    if value.startswith(_STATIC_PATH_PREFIX):
        return value[len(_STATIC_PATH_PREFIX) :]
    scheme, separator, remainder = value.partition(":")
    if separator and scheme in {"http", "https"} and remainder.startswith("//"):
        authority_start = len(scheme) + len(separator) + 2
    elif value.startswith("//"):
        authority_start = 2
    else:
        return None
    # Discovery recognizes insecure advertisements so static_url can reject
    # them; it never upgrades them or authorizes an HTTP request. Preserve the
    # required character before the static path without normalizing the URL.
    start = value.find(_STATIC_PATH_PREFIX, authority_start + 1)
    return value[start + len(_STATIC_PATH_PREFIX) :] if start >= 0 else None


def _has_script_suffix(suffix: str) -> bool:
    # A query/fragment may itself contain .js, as in the original matcher.
    return len(suffix) > 3 and (
        suffix.endswith(".js") or ".js?" in suffix[1:] or ".js#" in suffix[1:]
    )


@dataclass(frozen=True, slots=True, repr=False)
class PublicSearchConfig:
    """A non-repr credential with no global or disk cache."""

    key: str


@dataclass(slots=True)
class OperationBudget:
    """Physical requests and decoded bytes shared across attempts and redirects."""

    max_bytes: int
    max_requests: int
    seconds: float
    is_shutdown_requested: Callable[[], bool] = lambda: False
    consumed_bytes: int = 0
    request_count: int = 0
    deadline: float = field(init=False)

    def __post_init__(self) -> None:
        self.deadline = monotonic() + self.seconds

    def check(self, delay: float = 0.0) -> float:
        if self.is_shutdown_requested():
            raise FeedFetchInterruptedError
        remaining = self.deadline - monotonic()
        if remaining <= delay:
            raise FeedFetchError(SETEC_LABEL, "OperationDeadlineExceeded")
        return remaining

    def before_request(self) -> float:
        remaining = self.check()
        if self.request_count >= self.max_requests:
            raise FeedFetchError(SETEC_LABEL, "SearchRequestLimitExceeded")
        if self.consumed_bytes >= self.max_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
        self.request_count += 1
        return min(30.0, remaining)

    def charge(self, size: int) -> None:
        self.consumed_bytes += size
        self.check()
        if self.consumed_bytes > self.max_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")


def static_url(value: str) -> str:
    """Validate a path before normalization can hide traversal."""
    # Next explicitly advertises these literal paths relative to /_next/.
    # This is normalization of an advertised path, not import reconstruction.
    if value.startswith(_CHUNK_PATH_PREFIX):
        value = "/_next/" + value
    if any(part in {".", ".."} for part in value.split("/")) or any(
        ord(char) <= 32 for char in value
    ):
        raise FeedFetchError(SETEC_LABEL, "InvalidPublicAsset")
    url = urljoin(HOMEPAGE, value)
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname == SETEC_HOST
            and parsed.port in {None, 443}
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
            and parsed.path.startswith(_STATIC_PATH_PREFIX)
            and parsed.path.endswith(".js")
            # Route placeholders are percent-encoded by Next. No other escape
            # is accepted: encoded traversal, separators and double encoding fail.
            and "%" not in re.sub(r"%5[bBdD]", "", parsed.path)
            and "\\" not in url
        )
    except ValueError:
        valid = False
    if not valid:
        raise FeedFetchError(SETEC_LABEL, "InvalidPublicAsset")
    return urlunsplit(("https", SETEC_HOST, parsed.path, parsed.query, ""))


class _Scripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            for name, value in attrs:
                if name == "src" and value is not None:
                    self.sources.append(value)


def advertised_assets(content: bytes) -> tuple[str, ...]:
    text = content.decode("utf-8").replace('\\"', '"')
    parser = _Scripts()
    parser.feed(text)
    candidates = [
        *parser.sources,
        *(unescape(path) for path in _advertised_paths(text)),
    ]
    accepted: dict[str, None] = {}
    for candidate in candidates:
        try:
            host = urlsplit(candidate).hostname
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidPublicAsset") from None
        # External analytics scripts are normal homepage content, not candidates.
        # They are never fetched; own-origin candidates still fail closed.
        if host is not None and host != SETEC_HOST:
            continue
        accepted[static_url(candidate)] = None
    assets = tuple(accepted)
    if not assets or len(assets) > MAX_ASSETS:
        raise FeedFetchError(SETEC_LABEL, "PublicAssetLimitExceeded")
    return assets


def extract_configs(content: bytes) -> set[str]:
    return {match[4] for match in _CONSTRUCTOR.finditer(content.decode("utf-8"))}
