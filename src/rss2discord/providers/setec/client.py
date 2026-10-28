"""Secure, bounded search retrieval for the Setec catalog."""

from collections.abc import Iterator
from dataclasses import dataclass
from time import monotonic
from urllib.parse import urljoin, urlsplit

import requests

from rss2discord.providers.setec.catalog_bounds import (
    MAX_SETEC_REDIRECTS,
    SETEC_LABEL,
    SETEC_SEARCH_URL,
    SETEC_STREAM_CHUNK_BYTES,
    SETEC_USER_AGENT,
    SetecSearchRequest,
)
from rss2discord.providers.setec.public_config import (
    DISCOVERY_SECONDS,
    HOMEPAGE,
    MAX_DISCOVERY_BYTES,
    MAX_DISCOVERY_REQUESTS,
    MAX_DISCOVERY_RESPONSE_BYTES,
    OperationBudget,
    PublicSearchConfig,
    advertised_assets,
    extract_configs,
    static_url,
)
from rss2discord.retries import parse_retry_after
from rss2discord.transports.base import FeedFetchError

SETEC_PUBLIC_FILTER = "status = 'published' AND is_web_active = 'true'"


def public_search_body(body: dict[str, object]) -> dict[str, object]:
    """Intersect every query with the explicitly adopted public-webstore scope.

    Meilisearch filter arrays AND their elements (nested arrays remain OR groups),
    so preserve that structure rather than flattening or interpolating it.
    """
    scoped = dict(body)
    existing = scoped.get("filter")
    if existing is None:
        scoped["filter"] = SETEC_PUBLIC_FILTER
    elif isinstance(existing, str):
        scoped["filter"] = f"({SETEC_PUBLIC_FILTER}) AND ({existing})"
    elif isinstance(existing, list):
        scoped["filter"] = [SETEC_PUBLIC_FILTER, *existing]
    else:
        raise FeedFetchError(SETEC_LABEL, "InvalidSearchFilter")
    return scoped


@dataclass(frozen=True, slots=True)
class FetchedSetecSearch:
    """A raw search response body with the bytes consumed for its request chain."""

    content: bytes
    response_bytes: int


class SetecSearchClient:
    """Query the Setec search index without trusting redirects or response sizes."""

    def __init__(self, budget: OperationBudget | None = None) -> None:
        self.budget = budget or OperationBudget(32 * 1024 * 1024, 1000, 150.0)
        self._config: PublicSearchConfig | None = None
        self._discovery_started: float | None = None
        self._discovery_requests = 0
        self._discovery_bytes = 0

    def resolve_config(self) -> PublicSearchConfig:
        """Fetch the complete advertised candidate set before accepting uniqueness."""
        if self._config is not None:
            return self._config
        if self._discovery_started is None:
            self._discovery_started = monotonic()
        try:
            homepage = self._get_public(HOMEPAGE, homepage=True)
            keys: set[str] = set()
            for asset in advertised_assets(homepage):
                keys.update(extract_configs(self._get_public(asset)))
            self._discovery_remaining()
            if len(keys) != 1:
                raise FeedFetchError(SETEC_LABEL, "InvalidPublicSearchConfig")
            self._config = PublicSearchConfig(keys.pop())
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidPublicSearchConfig") from None
        else:
            return self._config

    def _discovery_remaining(self) -> float:
        if self._discovery_started is None:
            raise FeedFetchError(SETEC_LABEL, "InvalidPublicSearchConfig")
        remaining = DISCOVERY_SECONDS - (monotonic() - self._discovery_started)
        if remaining <= 0:
            raise FeedFetchError(SETEC_LABEL, "PublicDiscoveryDeadlineExceeded")
        return min(remaining, self.budget.check())

    def guard_retry(self, delay: float) -> None:
        """Reject backoff that would outlive either enclosing deadline."""
        self.budget.check(delay)
        if (
            self._config is None
            and self._discovery_started is not None
            and self._discovery_remaining() <= delay
        ):
            raise FeedFetchError(SETEC_LABEL, "PublicDiscoveryDeadlineExceeded")

    def _get_public(self, url: str, *, homepage: bool = False) -> bytes:
        try:
            for _ in range(MAX_SETEC_REDIRECTS + 1):
                timeout = self._before_public_request()
                with requests.get(
                    url,
                    headers={"User-Agent": SETEC_USER_AGENT},
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    content = self._read_public_content(response)
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
                        url = _same_origin_redirect_url(url, location)
                        if homepage and url != HOMEPAGE:
                            raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
                        if not homepage:
                            url = static_url(url)
                        continue
                    _check_status(response)
                    return content
            raise FeedFetchError(SETEC_LABEL, "TooManyRedirects")
        except requests.RequestException as error:
            raise FeedFetchError(
                SETEC_LABEL,
                type(error).__name__,
                retryable=isinstance(
                    error,
                    (
                        requests.ConnectionError,
                        requests.Timeout,
                        requests.exceptions.ChunkedEncodingError,
                    ),
                ),
            ) from None

    def _before_public_request(self) -> float:
        if self._discovery_requests >= MAX_DISCOVERY_REQUESTS:
            raise FeedFetchError(SETEC_LABEL, "PublicDiscoveryRequestLimitExceeded")
        remaining = self._discovery_remaining()
        timeout = min(remaining, self.budget.before_request())
        self._discovery_requests += 1
        return timeout

    def _read_public_content(self, response: requests.Response) -> bytes:
        content = bytearray()
        for chunk in response.iter_content(chunk_size=SETEC_STREAM_CHUNK_BYTES):
            self.budget.charge(len(chunk))
            self._discovery_bytes += len(chunk)
            self._discovery_remaining()
            if self._discovery_bytes > MAX_DISCOVERY_BYTES:
                raise FeedFetchError(SETEC_LABEL, "PublicDiscoveryTooLarge")
            if len(content) + len(chunk) > MAX_DISCOVERY_RESPONSE_BYTES:
                raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
            content.extend(chunk)
        self._discovery_remaining()
        return bytes(content)

    def build_search_url(self, url: str) -> str:
        """Validate a configured catalog URL and return the Setec search endpoint."""
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            username = parsed.username
            password = parsed.password
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme not in {"http", "https"}
            or hostname is None
            or username is not None
            or password is not None
        ):
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl")
        return SETEC_SEARCH_URL

    def search(
        self,
        search_url: str,
        request: SetecSearchRequest,
    ) -> FetchedSetecSearch:
        """Run one search while enforcing redirect and response-byte bounds."""
        if search_url != SETEC_SEARCH_URL:
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl")
        config = self.resolve_config()
        body = public_search_body(dict(request.body))
        current_url = search_url
        try:
            response_bytes = 0
            for _ in range(MAX_SETEC_REDIRECTS + 1):
                timeout = self.budget.before_request()
                with requests.post(
                    current_url,
                    json=body,
                    headers={
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {config.key}",
                        "User-Agent": SETEC_USER_AGENT,
                    },
                    timeout=timeout,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        redirect_content = _read_content(
                            response,
                            request,
                            response_bytes=response_bytes,
                            budget=self.budget,
                        )
                        response_bytes += len(redirect_content)
                        if location is None:
                            raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
                        current_url = _same_origin_redirect_url(current_url, location)
                        if (
                            urlsplit(current_url).path
                            != urlsplit(SETEC_SEARCH_URL).path
                        ):
                            raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
                        continue
                    response_content = _read_content(
                        response,
                        request,
                        response_bytes=response_bytes,
                        budget=self.budget,
                    )
                    response_bytes += len(response_content)
                    _check_status(response)
                    break
            else:
                raise FeedFetchError(SETEC_LABEL, "TooManyRedirects") from None
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl") from None
        except (
            requests.ConnectionError,
            requests.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ) as error:
            raise FeedFetchError(
                SETEC_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(SETEC_LABEL, type(error).__name__) from None
        return FetchedSetecSearch(
            content=response_content,
            response_bytes=response_bytes,
        )


def _same_origin_redirect_url(current_url: str, location: str) -> str:
    if (
        "\\" in location
        or any(ord(char) <= 32 for char in location)
        or any(part in {".", ".."} for part in location.split("/"))
    ):
        raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
    redirected_url = urljoin(current_url, location)
    try:
        current = urlsplit(current_url)
        redirected = urlsplit(redirected_url)
        default_port = 443 if current.scheme == "https" else 80
        current_port = current.port or default_port
        redirected_port = redirected.port or default_port
    except ValueError:
        raise FeedFetchError(SETEC_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != current.scheme
        or redirected.hostname != current.hostname
        or redirected_port != current_port
        or redirected.username is not None
        or redirected.password is not None
        or bool(redirected.fragment)
    ):
        raise FeedFetchError(SETEC_LABEL, "InvalidRedirect")
    return redirected_url


def _read_content(
    response: requests.Response,
    request: SetecSearchRequest,
    *,
    response_bytes: int,
    budget: OperationBudget,
) -> bytes:
    remaining_global_scan_bytes = request.remaining_scan_response_bytes - response_bytes
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > request.max_single_response_bytes:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        if declared_bytes > remaining_global_scan_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(chunk_size=SETEC_STREAM_CHUNK_BYTES)
    for chunk in chunks:
        budget.charge(len(chunk))
        if len(content) + len(chunk) > request.max_single_response_bytes:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        if len(content) + len(chunk) > remaining_global_scan_bytes:
            raise FeedFetchError(SETEC_LABEL, "ScanResponseTooLarge")
        content.extend(chunk)
    budget.check()
    return bytes(content)


def _check_status(response: requests.Response) -> None:
    if response.status_code >= 400:
        status = response.status_code
        raise FeedFetchError(
            SETEC_LABEL,
            "HTTPError",
            status_code=status,
            retryable=status in {408, 429} or 500 <= status < 600,
            retry_after=parse_retry_after(response.headers.get("Retry-After")),
        )
