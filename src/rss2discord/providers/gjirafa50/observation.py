"""Bounded public-detail observations, independent of enrollment and delivery.

A scalar confirms identity/currency/price for one response, not availability or
catalog coverage. Variant and fractional-MKD display ambiguity fail closed.
"""

import math
import re
import time
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from types import TracebackType
from typing import Self
from urllib.parse import urljoin, urlsplit

import certifi
from bs4 import BeautifulSoup
from curl_cffi import CurlOpt, requests
from curl_cffi.curl import CURL_WRITEFUNC_ERROR

from rss2discord.gjirafa_observation_models import (
    ObservationFetchError,
    ObservationItem,
    ScalarObservation,
)
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.providers.gjirafa50.session import (
    CurlCffiGjirafa50Response,
    _DeleteRoutingCookie,
    _routing_cookie,
    _SetRoutingCookie,
)

_HOSTS = frozenset({"gjirafa50.com", "gjirafa50.mk"})
_RESERVED = frozenset(
    {
        "admin",
        "files",
        "api",
        "product",
        "category",
        "account",
        "login",
        "search",
        "customer",
        "register",
        "cart",
        "checkout",
        "order",
        "orders",
        "download",
        "upload",
        "logout",
        "wishlist",
        "robots",
        "sitemap",
        "js",
        "css",
        "content",
    },
)
_USER_AGENT = "Mozilla/5.0 (compatible; rss2discord/0.1)"
_RESPONSE_LIMIT = 4 * 1024 * 1024
_GROUPED_NUMBER = r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)"


def _detail_url(value: str, host: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (
            len(value) <= 2048
            and not any(
                ord(char) <= 32 or ord(char) == 127 or char == "\\" for char in value
            )
            and parsed.scheme == "https"
            and parsed.netloc == host
            and parsed.hostname == host
            and parsed.port is None
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9-]*", parsed.path) is not None
            and parsed.path[1:].lower().split("-")[0] not in _RESERVED
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("InvalidDetailUrl")
    return value


def validate_observation_item(item: ObservationItem) -> None:
    """Validate a manifest item without network access or unsafe value echoing."""
    if (
        not isinstance(item.feed_id, str)
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,119}", item.feed_id) is None
        or not isinstance(item.product_id, str)
        or re.fullmatch(r"[1-9]\d{0,19}", item.product_id, flags=re.ASCII) is None
        or not isinstance(item.host, str)
        or item.host not in _HOSTS
    ):
        raise ValueError("InvalidObservationItem")
    if item.url is not None:
        if not isinstance(item.url, str):
            raise ValueError("InvalidDetailUrl")
        _detail_url(item.url, item.host)


def _scalar_guard(valid: bool) -> None:
    if not valid:
        raise ObservationFetchError("ScalarContractRejected")


def parse_scalar_observation(
    content: bytes,
    item: ObservationItem,
    canonical_url: str | None = None,
    observed_at: int | None = None,
) -> ScalarObservation:
    """Parse only the primary exact-ID scalar, never a recommendation or zero."""
    validate_observation_item(item)
    try:
        final_url = canonical_url if canonical_url is not None else item.url
        _scalar_guard(final_url is not None)
        assert final_url is not None  # noqa: S101
        _detail_url(final_url, item.host)
        _scalar_guard(isinstance(content, bytes) and len(content) <= _RESPONSE_LIMIT)
        _scalar_guard(content.count(b"<") <= 30_000)
        text = content.decode("utf-8", errors="strict")
        soup = BeautifulSoup(text, "html.parser")
        links = soup.select("link[rel=canonical][href]")
        canonical = {
            _detail_url(urljoin(final_url, str(link["href"])), item.host)
            for link in links
        }
        _scalar_guard(canonical == {final_url})
        mains = soup.select(".product-essential")
        _scalar_guard(len(mains) == 1)
        main = mains[0]
        identifiers = {
            str(tag["data-productid"]) for tag in main.select("[data-productid]")
        }
        _scalar_guard(identifiers == {item.product_id})
        _scalar_guard(
            not main.select(
                '[id^="product_attribute"]',
            ),
        )
        prices = main.select(".product-price-" + item.product_id)
        _scalar_guard(len(prices) == 1)
        currency = "EUR" if item.host == "gjirafa50.com" else "MKD"
        suffix = r" €" if currency == "EUR" else r" MKD\."
        match = re.fullmatch(
            _GROUPED_NUMBER + r"(?:\.[0-9]{2})?" + suffix,
            prices[0].get_text(" ", strip=True),
        )
        _scalar_guard(match is not None)
        displayed = prices[0].get_text(" ", strip=True).split(" ")[0].replace(",", "")
        _scalar_guard(len(displayed) <= 16)
        amount = Decimal(displayed)
        canonicalize_price_amount(amount)
        _scalar_guard(amount > 0 and amount == amount.quantize(Decimal("0.01")))
        products = [
            tag
            for tag in soup.select("[itemscope]")
            if tag.get("itemtype") == "https://schema.org/Product"
        ]
        _scalar_guard(len(products) == 1)
        numeric = products[0].select("[itemprop=price][content]")
        currencies = products[0].select("[itemprop=priceCurrency][content]")
        _scalar_guard(len(numeric) == len(currencies) == 1)
        raw = str(numeric[0]["content"])
        _scalar_guard(
            len(raw) <= 20
            and re.fullmatch(r"\d+(?:\.\d{1,4})?", raw, flags=re.ASCII) is not None,
        )
        _scalar_guard(currencies[0]["content"] == currency and Decimal(raw) == amount)
        timestamp = int(time.time()) if observed_at is None else observed_at
        _scalar_guard(type(timestamp) is int and timestamp > 0)
        return ScalarObservation(amount, currency, final_url, timestamp)
    except ObservationFetchError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, InvalidOperation):
        raise ObservationFetchError("ScalarContractRejected") from None


class _ResponseContent:
    """Charge bytes before capture and retain the exact callback abort."""

    def __init__(
        self,
        active: Callable[[], None],
        charge: Callable[[int, int], None],
    ) -> None:
        self.body = bytearray()
        self.headers: list[bytes] = []
        self.abort: ObservationFetchError | None = None
        self._bytes = 0
        self._active, self._charge = active, charge

    def receive(self, chunk: bytes, *, header: bool = False) -> int:
        try:
            self._active()
            self._bytes += len(chunk)
            self._charge(len(chunk), self._bytes)
        except ObservationFetchError as error:
            self.abort = error
            return CURL_WRITEFUNC_ERROR
        if header:
            self.headers.append(chunk)
        else:
            self.body.extend(chunk)
        return len(chunk)


class GjirafaObservationClient:
    """Serial plain-curl boundary. Construction and parsing never issue HTTP."""

    def __init__(
        self,
        *,
        max_requests: int = 200,
        max_bytes: int = 64 * 1024 * 1024,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            type(max_requests) is not int
            or not 1 <= max_requests <= 200
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= 64 * 1024 * 1024
        ):
            raise ValueError("InvalidObservationBudget")
        self.request_count = 0
        self.response_bytes = 0
        self._max_requests, self._max_bytes = max_requests, max_bytes
        self._shutdown = is_shutdown_requested
        self._clock, self._wall, self._sleep = clock, wall_clock, sleep
        self._deadline = clock() + 180
        self._last: dict[str, float] = {}
        self._blocked: dict[str, int] = {}
        self._routing: dict[str, _SetRoutingCookie] = {}
        self._fatal: str | None = None
        self._closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True
        self._routing.clear()

    def _fail_fatal(self, reason: str) -> None:
        self._fatal = reason
        raise ObservationFetchError(reason, fatal=True)

    def _active(self) -> None:
        if self._fatal:
            raise ObservationFetchError(self._fatal, fatal=True)
        if self._closed or self._shutdown():
            self._fail_fatal("ObservationAborted")
        if self._clock() >= self._deadline:
            self._fail_fatal("ObservationDeadlineExceeded")

    def _cooldown(self, host: str, reason: str, retry_after: str | None = None) -> None:
        now = int(self._wall())
        until = now + 6 * 3600
        if retry_after and len(retry_after) <= 128:
            if (
                retry_after.isascii()
                and retry_after.isdigit()
                and len(retry_after) <= 12
            ):
                until = now + int(retry_after)
            else:
                try:
                    parsed = parsedate_to_datetime(retry_after)
                    if parsed.tzinfo is not None:
                        until = max(now, math.ceil(parsed.timestamp()))
                except (ValueError, TypeError, OverflowError):
                    pass
        self._blocked[host] = until
        raise ObservationFetchError(reason, blocked_until=until, stop_host=True)

    def _before_request(self, host: str) -> None:
        self._active()
        if host in self._last:
            delay = 1 - (self._clock() - self._last[host])
            if delay > 0:
                self._sleep(delay)
        self._active()
        if self.request_count >= self._max_requests:
            self._fail_fatal("ObservationRequestLimitExceeded")
        self.request_count += 1
        self._last[host] = self._clock()

    def _charge_bytes(self, count: int, local_bytes: int) -> None:
        self.response_bytes += count
        if local_bytes > _RESPONSE_LIMIT:
            self._fail_fatal("ObservationResponseLimitExceeded")
        if self.response_bytes > self._max_bytes:
            self._fail_fatal("ObservationByteLimitExceeded")

    def _request_headers(self, host: str) -> dict[str, str]:
        headers = {"User-Agent": _USER_AGENT, "Accept": "text/html"}
        route = self._routing.get(host)
        if route is not None:
            if route.expires_at is not None and self._clock() >= route.expires_at:
                self._routing.pop(host, None)
            else:
                headers["Cookie"] = f"gjs={route.value}"
        return headers

    def _transfer(self, url: str, host: str) -> tuple[int, dict[str, str], bytes]:
        self._before_request(host)
        content = _ResponseContent(self._active, self._charge_bytes)
        headers = self._request_headers(host)
        session: requests.Session[requests.Response] = requests.Session(
            trust_env=False,
            discard_cookies=True,
            default_headers=False,
            curl_options={
                CurlOpt.TIMEOUT_MS: max(
                    1,
                    math.floor(min(20, self._deadline - self._clock()) * 1000),
                ),
                CurlOpt.PROXY: "",
                CurlOpt.NETRC: 0,
                CurlOpt.CAINFO: certifi.where(),
                CurlOpt.HEADERFUNCTION: lambda line: content.receive(line, header=True),
            },
        )
        try:
            try:
                raw_response = session.get(
                    url,
                    headers=headers,
                    allow_redirects=False,
                    content_callback=content.receive,
                )
            except Exception:
                if content.abort is not None:
                    raise content.abort from None
                self._active()
                raise ObservationFetchError("ObservationTransportError") from None
            if content.abort is not None:
                raise content.abort
            self._active()
            response = CurlCffiGjirafa50Response(raw_response, content.headers)
            status, response_headers = response.status_code, dict(response.headers)
        finally:
            session.close()
        self._response_policy(host, status, response_headers, content)
        return status, response_headers, bytes(content.body)

    def _response_policy(
        self,
        host: str,
        status: int,
        response_headers: dict[str, str],
        content: _ResponseContent,
    ) -> None:
        if status in {403, 429}:
            self._cooldown(
                host,
                "ObservationAccessBlocked",
                response_headers.get("retry-after"),
            )
        if any(
            token in bytes(content.body).lower()
            for token in (
                b"cf-chl-",
                b"challenge-platform",
                b"captcha",
                b"just a moment",
            )
        ):
            self._cooldown(host, "ObservationChallengeDetected")
        directive = _routing_cookie(content.headers, host)
        if isinstance(directive, _SetRoutingCookie):
            current = self._routing.get(host)
            if current is not None and current.value != directive.value:
                self._cooldown(host, "ObservationRoutingChanged")
            self._routing[host] = directive
        elif isinstance(directive, _DeleteRoutingCookie):
            self._routing.pop(host, None)

    def fetch(self, item: ObservationItem) -> ScalarObservation:
        validate_observation_item(item)
        self._active()
        if item.url is None:
            raise ObservationFetchError("ObservationUrlUnknown")
        if item.host in self._blocked:
            raise ObservationFetchError(
                "ObservationHostStopped",
                blocked_until=self._blocked[item.host],
                stop_host=True,
            )
        current = item.url
        for redirect in range(4):
            status, headers, content = self._transfer(current, item.host)
            if 300 <= status < 400:
                try:
                    if redirect == 3 or not headers.get("location"):
                        raise ValueError("InvalidRedirect")  # noqa: TRY301
                    current = _detail_url(
                        urljoin(current, headers["location"]),
                        item.host,
                    )
                except ValueError:
                    self._cooldown(item.host, "ObservationUnsafeRedirect")
                continue
            if status != 200:
                raise ObservationFetchError("ObservationHttpError")
            self._active()
            result = parse_scalar_observation(content, item, current, int(self._wall()))
            self._active()
            return result
        self._cooldown(item.host, "ObservationUnsafeRedirect")
        raise AssertionError("unreachable")
