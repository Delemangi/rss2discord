"""Synthetic observation contracts; no live requests or retained product data."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from curl_cffi import CurlOpt

from rss2discord.gjirafa_observation_models import (
    ObservationFetchError,
    ObservationItem,
)
from rss2discord.providers.gjirafa50 import observation as obs


def item(
    host: str = "gjirafa50.com",
    product_id: str = "123",
    url: str | None = "default",
) -> ObservationItem:
    return ObservationItem(
        "test-feed",
        host,
        product_id,
        f"https://{host}/sample-product" if url == "default" else url,
    )


def html(
    host: str = "gjirafa50.com",
    *,
    visible: str | None = None,
    raw: str = "12.50",
    extra: str = "",
) -> bytes:
    currency = "EUR" if host.endswith(".com") else "MKD"
    displayed = visible or ("12.50 €" if currency == "EUR" else "12.50 MKD.")
    return f"""<link rel="canonical" href="https://{host}/sample-product">
    <link rel="canonical" href="https://{host}/sample-product">
    <div itemscope itemtype="https://schema.org/Product">
    <meta itemprop="price" content="{raw}"><meta itemprop="priceCurrency" content="{currency}"></div>
    <div class="product-essential"><button data-productid="123"></button>
    <div class="product-price">Old99.00 Tax9.00<span class="product-price-123">{displayed}</span></div>{extra}</div>
    <div class="recommendations"><span class="product-price-999">999.00 €</span></div>""".encode()


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def clock(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.now += delay


def fake_sessions(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, list[bytes], bytes]],
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    class Session:
        def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401
            self.options = kwargs

        def get(self, url: str, **kwargs: Any) -> SimpleNamespace:  # noqa: ANN401
            calls.append(dict(url=url, options=self.options, **kwargs))
            status, headers, body = responses.pop(0)
            callback: Callable[[bytes], int] = self.options["curl_options"][
                CurlOpt.HEADERFUNCTION
            ]
            callback(f"HTTP/2 {status}\r\n".encode())
            for line in headers:
                callback(line)
            kwargs["content_callback"](body)
            return SimpleNamespace(status_code=status)

        def close(self) -> None:
            pass

    monkeypatch.setattr(obs.requests, "Session", Session)
    return calls


@pytest.mark.parametrize("host", ["gjirafa50.com", "gjirafa50.mk"])
def test_scalar_exact_primary(host: str) -> None:
    value = obs.parse_scalar_observation(html(host), item(host), observed_at=1000)
    assert str(value.amount) == "12.50"
    assert value.observed_at == 1000


@pytest.mark.parametrize("slug", ["sample-product-", "sample--product--"])
def test_opaque_hyphen_slug_canonical_match(slug: str) -> None:
    url = f"https://gjirafa50.com/{slug}"
    target = item(url=url)
    obs.validate_observation_item(target)
    content = html().replace(b"/sample-product", f"/{slug}".encode())
    assert obs.parse_scalar_observation(content, target).canonical_url == url
    with pytest.raises(ObservationFetchError, match="ScalarContractRejected"):
        obs.parse_scalar_observation(html(), target)


def test_dotted_feed_id_is_valid() -> None:
    target = ObservationItem("test.feed-1", "gjirafa50.com", "123", item().url)
    obs.validate_observation_item(target)
    assert obs.parse_scalar_observation(html(), target).currency == "EUR"


@pytest.mark.parametrize("tag", ["textarea", "button", "div"])
def test_any_attribute_element_blocks_scalar(tag: str) -> None:
    content = html(extra=f'<{tag} id="product_attribute_7"></{tag}>')
    with pytest.raises(ObservationFetchError, match="ScalarContractRejected"):
        obs.parse_scalar_observation(content, item())


@pytest.mark.parametrize(
    "url",
    [
        "http://gjirafa50.com/sample-product",
        "https://evil.test/sample-product",
        "https://secret@gjirafa50.com/sample-product",
        "https://gjirafa50.com:443/sample-product",
        "https://gjirafa50.com/admin",
        "https://gjirafa50.com/api-private",
        "https://gjirafa50.com/product/123",
        "https://gjirafa50.com/%252e%252e",
        "https://gjirafa50.com/a%2fb",
        "https://gjirafa50.com/a?secret=x",
        "https://gjirafa50.com/a#b",
        "https://gjirafa50.com/a\\b",
        "https://gjirafa50.com/",
    ],
)
def test_manifest_url_validation(url: str) -> None:
    with pytest.raises(ValueError, match="InvalidDetailUrl"):
        obs.validate_observation_item(item(url=url))


@pytest.mark.parametrize(
    "content",
    [
        html().replace(b'data-productid="123"', b'data-productid="456"'),
        html().replace(b'content="EUR"', b'content="MKD"'),
        html().replace(
            b'href="https://gjirafa50.com/sample-product"',
            b'href="https://evil.test/sample-product"',
        ),
        html(extra='<input id="product_attribute_1">'),
        html(visible="0.00 €", raw="0.00"),
        html(visible="12-13 €"),
        html().replace(b"product-price-123", b"product-price-456"),
        html("gjirafa50.mk", visible="13 MKD.", raw="12.50"),
        b"not html",
        b"<" * 30001,
    ],
)
def test_scalar_rejects_ambiguous(content: bytes) -> None:
    target = item("gjirafa50.mk") if b"gjirafa50.mk" in content else item()
    with pytest.raises(ObservationFetchError, match="ScalarContractRejected"):
        obs.parse_scalar_observation(content, target)


def test_unknown_url_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_sessions(monkeypatch, [])
    client = obs.GjirafaObservationClient()
    with pytest.raises(ObservationFetchError, match="ObservationUrlUnknown"):
        client.fetch(item(url=None))
    assert not calls


def test_redirects_physical_headers_and_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_sessions(
        monkeypatch,
        [(302, [b"Location: /sample-product\r\n"], b""), (200, [], html())],
    )
    clock = Clock()
    with obs.GjirafaObservationClient(clock=clock.clock, sleep=clock.sleep) as client:
        assert client.fetch(item()).currency == "EUR"
        assert client.request_count == 2
        assert client.response_bytes > len(html())
        assert clock.now >= 101
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("status", "retry", "expected"),
    [(403, None, 22600), (429, b"60", 1060), (429, b"invalid", 22600)],
)
def test_host_cooldown_no_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    retry: bytes | None,
    expected: int,
) -> None:
    headers = [] if retry is None else [b"Retry-After: " + retry + b"\r\n"]
    calls = fake_sessions(monkeypatch, [(status, headers, b"")])
    client = obs.GjirafaObservationClient(wall_clock=lambda: 1000)
    for _ in range(2):
        with pytest.raises(ObservationFetchError) as error:
            client.fetch(item())
        assert error.value.stop_host
        assert error.value.blocked_until == expected
    assert len(calls) == 1


def test_routing_and_plain_tls_options(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_sessions(
        monkeypatch,
        [(200, [b"Set-Cookie: gjs=node-a; Path=/\r\n"], html()), (200, [], html())],
    )
    clock = Clock()
    client = obs.GjirafaObservationClient(clock=clock.clock, sleep=clock.sleep)
    client.fetch(item())
    client.fetch(item())
    assert "Cookie" not in calls[0]["headers"]
    assert calls[1]["headers"]["Cookie"] == "gjs=node-a"
    assert "impersonate" not in calls[0]
    options = calls[0]["options"]
    assert options["trust_env"] is False
    assert options["discard_cookies"] is True
    assert options["curl_options"][CurlOpt.NETRC] == 0
    assert options["curl_options"][CurlOpt.CAINFO]


def test_failed_physical_request_count(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_sessions(monkeypatch, [])
    client = obs.GjirafaObservationClient()
    # Fake failure occurs after dispatch; no automatic retry is possible.
    with pytest.raises(ObservationFetchError, match="ObservationTransportError"):
        client.fetch(item())
    assert client.request_count == 1
    assert len(calls) == 1


def test_byte_and_request_caps_sticky(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sessions(monkeypatch, [(200, [], html())])
    client = obs.GjirafaObservationClient(max_bytes=1)
    with pytest.raises(ObservationFetchError) as error:
        client.fetch(item())
    assert error.value.fatal
    fake_sessions(monkeypatch, [(200, [], html())])
    client = obs.GjirafaObservationClient(max_requests=1, sleep=lambda _: None)
    client.fetch(item())
    with pytest.raises(ObservationFetchError) as error:
        client.fetch(item())
    assert error.value.fatal


def test_parse_post_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sessions(monkeypatch, [(200, [], html())])
    clock = Clock()
    original = obs.parse_scalar_observation

    def parse(*args: Any, **kwargs: Any) -> obs.ScalarObservation:  # noqa: ANN401
        result = original(*args, **kwargs)
        clock.now += 181
        return result

    monkeypatch.setattr(obs, "parse_scalar_observation", parse)
    client = obs.GjirafaObservationClient(clock=clock.clock, sleep=clock.sleep)
    with pytest.raises(ObservationFetchError) as error:
        client.fetch(item())
    assert error.value.fatal


@pytest.mark.parametrize(
    "cookie",
    [
        b"gjs=node-a; Path=/private",
        b"gjs=node-a; Path=/; Domain=evil.test",
        b"gjs=INVALID_VALUE; Path=/",
        b"auth=anything; Path=/",
    ],
)
def test_invalid_routing_cookie_ignored(
    monkeypatch: pytest.MonkeyPatch,
    cookie: bytes,
) -> None:
    calls = fake_sessions(
        monkeypatch,
        [(200, [b"Set-Cookie: " + cookie + b"\r\n"], html()), (200, [], html())],
    )
    clock = Clock()
    client = obs.GjirafaObservationClient(clock=clock.clock, sleep=clock.sleep)
    client.fetch(item())
    client.fetch(item())
    assert "Cookie" not in calls[1]["headers"]


def test_routing_change_host_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_sessions(
        monkeypatch,
        [
            (200, [b"Set-Cookie: gjs=node-a; Path=/\r\n"], html()),
            (200, [b"Set-Cookie: gjs=node-b; Path=/\r\n"], html()),
        ],
    )
    clock = Clock()
    client = obs.GjirafaObservationClient(clock=clock.clock, sleep=clock.sleep)
    client.fetch(item())
    with pytest.raises(
        ObservationFetchError,
        match="ObservationRoutingChanged",
    ) as error:
        client.fetch(item())
    assert error.value.stop_host
    assert len(calls) == 2


def test_header_reset_and_cookie_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_sessions(
        monkeypatch,
        [
            (
                200,
                [
                    b"Set-Cookie: gjs=ignored; Path=/\r\n",
                    b"HTTP/2 200\r\n",
                    b"Set-Cookie: gjs=node-a; Path=/\r\n",
                ],
                html(),
            ),
            (200, [b"Set-Cookie: gjs=; Path=/; Max-Age=0\r\n"], html()),
            (200, [], html()),
        ],
    )
    clock = Clock()
    client = obs.GjirafaObservationClient(clock=clock.clock, sleep=clock.sleep)
    for _ in range(3):
        client.fetch(item())
    assert calls[1]["headers"]["Cookie"] == "gjs=node-a"
    assert "Cookie" not in calls[2]["headers"]


def test_challenge_and_unsafe_redirect_host_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for response in [
        (200, [], b"cf-chl-test"),
        (302, [b"Location: https://evil.test/a\r\n"], b""),
    ]:
        calls = fake_sessions(monkeypatch, [response])
        client = obs.GjirafaObservationClient()
        with pytest.raises(ObservationFetchError) as error:
            client.fetch(item())
        assert error.value.stop_host
        assert len(calls) == 1


def test_response_limit_and_shutdown(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_sessions(monkeypatch, [(200, [], b"x" * (4 * 1024 * 1024 + 1))])
    client = obs.GjirafaObservationClient()
    with pytest.raises(
        ObservationFetchError,
        match="ObservationResponseLimitExceeded",
    ) as error:
        client.fetch(item())
    assert error.value.fatal
    calls = fake_sessions(monkeypatch, [])
    client = obs.GjirafaObservationClient(is_shutdown_requested=lambda: True)
    with pytest.raises(ObservationFetchError) as error:
        client.fetch(item())
    assert error.value.fatal
    assert not calls


def test_timestamp_and_multiple_product_rejected() -> None:
    for timestamp in (0, -1, True):
        with pytest.raises(ObservationFetchError):
            obs.parse_scalar_observation(html(), item(), observed_at=timestamp)
    with pytest.raises(ObservationFetchError):
        obs.parse_scalar_observation(
            html() + b'<div itemscope itemtype="https://schema.org/Product"></div>',
            item(),
        )
