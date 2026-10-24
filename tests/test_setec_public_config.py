"""Synthetic public-contract evidence; no downloaded keys or network access."""

import json
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

import pytest
import requests

from rss2discord.delivery_store import DeliveryStore
from rss2discord.providers.setec import catalog, public_config
from rss2discord.providers.setec import client as client_module
from rss2discord.providers.setec.catalog import SetecCatalogClient
from rss2discord.providers.setec.catalog_bounds import (
    SETEC_SEARCH_URL,
    SetecSearchRequest,
)
from rss2discord.providers.setec.client import (
    SETEC_PUBLIC_FILTER,
    SetecSearchClient,
    public_search_body,
)
from rss2discord.providers.setec.public_config import (
    HOMEPAGE,
    OperationBudget,
    advertised_assets,
    extract_configs,
    static_url,
)
from rss2discord.retries import FeedFetchInterruptedError, FetchRetryPolicy
from rss2discord.transports import FeedFetchError
from tests.setec_helpers import (
    CATALOG_URL,
    RecordingPost,
    StubResponse,
    count_payload,
    no_wait_fetch_retry_policy,
    price_payload,
    search_payload,
)
from tests.setec_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
)

KEY = "placeholder-public-key-one"
OTHER_KEY = "placeholder-public-key-two"


def constructor(key: str = KEY) -> bytes:
    return (
        f'let o=(0,a.m)("https://search.sp.solslab.dev/","{key}"),c="products";'
    ).encode()


def homepage(count: int = 35) -> bytes:
    # Mixed script src and escaped literal inline Next paths, including duplicates.
    return (
        '<script src="/_next/static/0.js"></script>'
        '<script>self.__next_f.push([1,"'
        + ",".join(f'\\"/_next/static/{i}.js\\"' for i in range(count))
        + '"])</script>'
    ).encode()


class PublicGet:
    def __init__(self, responses: Mapping[str, StubResponse] | None = None) -> None:
        self.responses = responses or {
            HOMEPAGE: StubResponse(homepage()),
            **{
                f"https://setec.mk/_next/static/{i}.js": StubResponse(
                    constructor() if i == 22 else b"/* no configuration */",
                )
                for i in range(35)
            },
        }
        self.urls: list[str] = []
        self.timeouts: list[float] = []

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
        stream: bool,
        allow_redirects: bool,
    ) -> AbstractContextManager[StubResponse]:
        assert "Authorization" not in headers
        assert stream
        assert not allow_redirects
        self.urls.append(url)
        self.timeouts.append(timeout)
        return nullcontext(self.responses[url])


def small_get(asset: bytes = constructor()) -> PublicGet:
    return PublicGet(
        {
            HOMEPAGE: StubResponse(homepage(1)),
            "https://setec.mk/_next/static/0.js": StubResponse(asset),
        },
    )


def request() -> SetecSearchRequest:
    return SetecSearchRequest({"q": ""}, 1024, 4096)


def test_complete_35_path_discovery_and_operation_local_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = PublicGet()
    post = RecordingPost([StubResponse(b"{}"), StubResponse(b"{}")])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    client = SetecSearchClient()
    client.search(SETEC_SEARCH_URL, request())
    client.search(SETEC_SEARCH_URL, request())
    assert len(get.urls) == 36  # Not truncated at 20 or at the first match.
    assert client.budget.request_count == 38
    assert post.headers[0]["Authorization"] == f"Bearer {KEY}"
    assert KEY not in repr(client.resolve_config())
    SetecSearchClient().resolve_config()
    assert len(get.urls) == 72


def test_conflicting_late_candidate_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    get = PublicGet()
    get.responses = dict(get.responses)
    get.responses["https://setec.mk/_next/static/34.js"] = StubResponse(
        constructor(OTHER_KEY),
    )
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(FeedFetchError, match="InvalidPublicSearchConfig"):
        SetecSearchClient().resolve_config()
    assert len(get.urls) == 36


def test_identical_duplicates_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    get = PublicGet()
    get.responses = dict(get.responses)
    get.responses["https://setec.mk/_next/static/34.js"] = StubResponse(constructor())
    monkeypatch.setattr(requests, "get", get)
    assert SetecSearchClient().resolve_config().key == KEY
    assert len(get.urls) == 36


@pytest.mark.parametrize(
    "body",
    [
        b'let unrelated="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";',
        constructor().replace(b"products", b"orders"),
        constructor().replace(b"search.sp.solslab.dev", b"evil.example"),
        constructor().replace(b',c="products"', b';other();let c="products"'),
    ],
)
def test_only_associated_allowlisted_tuple(body: bytes) -> None:
    assert not extract_configs(body)


def test_missing_configuration_rejects_without_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get(b"/* no key */")
    post = RecordingPost([])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match="InvalidPublicSearchConfig"):
        SetecSearchClient().search(SETEC_SEARCH_URL, request())
    assert not post.urls


def test_inline_dedupe_and_overflow() -> None:
    assert len(advertised_assets(homepage(35))) == 35
    assert len(advertised_assets(homepage(40))) == 40
    with pytest.raises(FeedFetchError, match="PublicAssetLimitExceeded"):
        advertised_assets(homepage(41))


@pytest.mark.parametrize(
    "url",
    [
        "http://setec.mk/_next/static/a.js",
        "https://evil.example/_next/static/a.js",
        "https://user@setec.mk/_next/static/a.js",
        "https://setec.mk:444/_next/static/a.js",
        "/_next/static/../a.js",
        "/_next/static/%2e%2e/a.js",
        "/_next/static/a.js#fragment",
        "/private/a.js",
    ],
)
def test_forbidden_assets(url: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidPublicAsset"):
        static_url(url)


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/_next/static/a.js",
        "https://user@setec.mk/_next/static/a.js",
        "http://setec.mk/_next/static/a.js",
        "/private/a.js",
    ],
)
def test_asset_redirect_rejected_before_wire(
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    get = small_get()
    get.responses = dict(get.responses)
    get.responses["https://setec.mk/_next/static/0.js"] = StubResponse(
        b"redirect",
        302,
        {"Location": location},
    )
    monkeypatch.setattr(requests, "get", get)
    client = SetecSearchClient()
    with pytest.raises(FeedFetchError):
        client.resolve_config()
    assert len(get.urls) == 2
    assert client.budget.consumed_bytes == len(homepage(1)) + len(b"redirect")


@pytest.mark.parametrize("status", [401, 403])
def test_auth_failure_has_no_refresh_fallback_or_retry(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    get = small_get()
    post = RecordingPost([StubResponse(b"denied", status)])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match=f"HTTP {status}") as caught:
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: False,
        )
    assert caught.value.status_code == status
    assert len(get.urls) == 2
    assert len(post.urls) == 1
    assert KEY not in str(caught.value)


def test_retry_reuses_key_and_charges_failed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    post = RecordingPost(
        [StubResponse(b"failed", 503), StubResponse(count_payload([]))],
    )
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    budget = OperationBudget(100000, 10, 300)
    client = SetecSearchClient(budget)
    no_wait_fetch_retry_policy().execute(
        lambda: client.search(SETEC_SEARCH_URL, request()),
    )
    assert len(get.urls) == 2
    assert budget.request_count == 4
    assert budget.consumed_bytes == (
        len(homepage(1)) + len(constructor()) + len(b"failed") + len(count_payload([]))
    )


def test_failed_attempts_do_not_reset_catalog_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(catalog, "MAX_SETEC_SEARCH_REQUESTS", 3)
    get = small_get()
    post = RecordingPost([StubResponse(b"failed", 503)])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match="SearchRequestLimitExceeded"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: False,
        )
    assert len(post.urls) == 1


def test_shutdown_before_discovery_prevents_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    with pytest.raises(FeedFetchInterruptedError):
        SetecSearchClient(OperationBudget(1000, 10, 300, lambda: True)).resolve_config()
    assert not get.urls


def test_deadline_and_retry_delay_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(public_config, "monotonic", lambda: now[0])
    budget = OperationBudget(1000, 10, 300)
    now[0] = 299
    assert budget.before_request() == 1
    slept: list[float] = []

    def sleep(seconds: float) -> bool:
        slept.append(seconds)
        return True

    retry = FetchRetryPolicy(sleep, lambda error, delay: None)

    def fail() -> None:
        raise FeedFetchError("Setec", "HTTPError", retryable=True, retry_after=2)

    def guard(delay: float) -> None:
        budget.check(delay)

    with pytest.raises(FeedFetchError, match="OperationDeadlineExceeded"):
        retry.execute(fail, retry_guard=guard)
    assert not slept
    now[0] = 300
    with pytest.raises(FeedFetchError, match="OperationDeadlineExceeded"):
        budget.charge(1)


def test_global_streaming_byte_bound_includes_error_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    budget = OperationBudget(len(homepage(1)) + len(constructor()) + 3, 10, 300)
    client = SetecSearchClient(budget)
    post = RecordingPost([StubResponse(b"failure-body", 503)])
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match="ScanResponseTooLarge"):
        client.search(SETEC_SEARCH_URL, request())
    assert budget.consumed_bytes > budget.max_bytes
    assert budget.request_count == 3


def test_discovery_redirects_share_physical_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    get.responses = dict(get.responses)
    get.responses["https://setec.mk/_next/static/0.js"] = StubResponse(
        b"redirect",
        302,
        {"Location": "/_next/static/1.js"},
    )
    get.responses["https://setec.mk/_next/static/1.js"] = StubResponse(constructor())
    monkeypatch.setattr(requests, "get", get)
    client = SetecSearchClient(OperationBudget(10000, 2, 300))
    with pytest.raises(FeedFetchError, match="SearchRequestLimitExceeded"):
        client.resolve_config()
    assert len(get.urls) == 2
    assert client.budget.request_count == 2


def test_discovery_sublimit_counts_redirects(monkeypatch: pytest.MonkeyPatch) -> None:
    get = small_get()
    get.responses = dict(get.responses)
    get.responses["https://setec.mk/_next/static/0.js"] = StubResponse(
        b"redirect",
        302,
        {"Location": "/_next/static/1.js"},
    )
    monkeypatch.setattr(client_module, "MAX_DISCOVERY_REQUESTS", 2)
    monkeypatch.setattr(requests, "get", get)
    client = SetecSearchClient()
    with pytest.raises(FeedFetchError, match="PublicDiscoveryRequestLimitExceeded"):
        client.resolve_config()
    assert client.budget.request_count == 2


@pytest.mark.parametrize("limit", ["single", "total"])
def test_discovery_streaming_byte_sublimits(
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
) -> None:
    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    if limit == "single":
        monkeypatch.setattr(
            client_module,
            "MAX_DISCOVERY_RESPONSE_BYTES",
            len(homepage(1)) - 1,
        )
        expected = "ResponseTooLarge"
    else:
        monkeypatch.setattr(client_module, "MAX_DISCOVERY_BYTES", len(homepage(1)) + 1)
        expected = "PublicDiscoveryTooLarge"
    client = SetecSearchClient()
    with pytest.raises(FeedFetchError, match=expected):
        client.resolve_config()
    assert client.budget.consumed_bytes >= len(homepage(1))


def test_discovery_deadline_covers_streaming_and_retry_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: now[0])
    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    client = SetecSearchClient()
    client._discovery_started = 0
    now[0] = 119
    with pytest.raises(FeedFetchError, match="PublicDiscoveryDeadlineExceeded"):
        client.guard_retry(2)
    now[0] = 120
    with pytest.raises(FeedFetchError, match="PublicDiscoveryDeadlineExceeded"):
        client.resolve_config()
    assert not get.urls


def test_shutdown_while_streaming_charges_bytes_then_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    budget = OperationBudget(10000, 10, 300)
    client = SetecSearchClient(budget)
    client.resolve_config()
    before = budget.consumed_bytes
    post = RecordingPost([StubResponse(b"data")])
    monkeypatch.setattr(requests, "post", post)
    budget.is_shutdown_requested = lambda: bool(post.urls)
    with pytest.raises(FeedFetchInterruptedError):
        client.search(SETEC_SEARCH_URL, request())
    assert budget.consumed_bytes == before + len(b"data")
    assert len(post.urls) == 1


def test_search_redirects_share_budget_and_cannot_change_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    post = RecordingPost(
        [
            StubResponse(b"redirect", 302, {"Location": SETEC_SEARCH_URL + "?hop=1"}),
        ],
    )
    monkeypatch.setattr(requests, "post", post)
    client = SetecSearchClient(OperationBudget(10000, 3, 300))
    with pytest.raises(FeedFetchError, match="SearchRequestLimitExceeded"):
        client.search(SETEC_SEARCH_URL, request())
    assert len(post.urls) == 1
    assert client.budget.request_count == 3

    post = RecordingPost(
        [
            StubResponse(b"redirect", 302, {"Location": "/indexes/private/search"}),
        ],
    )
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match="InvalidRedirect"):
        SetecSearchClient().search(SETEC_SEARCH_URL, request())
    assert len(post.urls) == 1


@pytest.mark.parametrize(
    "source",
    [
        "http://setec.mk/_next/static/a.js",
        "https://evil.example/_next/static/a.js",
        "/_next/static/../a.js",
        "/_next/static/a.js#unsafe",
    ],
)
def test_advertised_unsafe_script_is_not_silently_skipped(source: str) -> None:
    with pytest.raises(FeedFetchError):
        advertised_assets(f'<script src="{source}"></script>'.encode())


def test_each_catalog_operation_discovers_fresh_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get = small_get()
    post = RecordingPost([StubResponse(b'{"hits":[]}'), StubResponse(b'{"hits":[]}')])
    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    catalog_client = SetecCatalogClient()
    assert catalog_client.fetch_latest_products(CATALOG_URL) == ()
    get.responses = dict(get.responses)
    get.responses["https://setec.mk/_next/static/0.js"] = StubResponse(
        constructor(OTHER_KEY),
    )
    assert catalog_client.fetch_latest_products(CATALOG_URL) == ()
    assert len(get.urls) == 4
    assert post.headers[0]["Authorization"] == f"Bearer {KEY}"
    assert post.headers[1]["Authorization"] == f"Bearer {OTHER_KEY}"


def test_partial_stream_failure_keeps_consumed_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenResponse(StubResponse):
        def iter_content(self, chunk_size: int) -> Iterator[bytes]:
            del chunk_size
            yield b"partial"
            raise requests.exceptions.ChunkedEncodingError(KEY)

    get = small_get()
    monkeypatch.setattr(requests, "get", get)
    client = SetecSearchClient()
    client.resolve_config()
    before = client.budget.consumed_bytes
    post = RecordingPost([BrokenResponse(b"")])
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match="ChunkedEncodingError") as caught:
        client.search(SETEC_SEARCH_URL, request())
    assert client.budget.consumed_bytes == before + len(b"partial")
    assert caught.value.retryable
    assert KEY not in str(caught.value)


def test_discovery_streaming_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    monkeypatch.setattr(client_module, "monotonic", lambda: now[0])

    class SlowResponse(StubResponse):
        def iter_content(self, chunk_size: int) -> Iterator[bytes]:
            del chunk_size
            now[0] = 120
            yield b"partial"

    get = PublicGet({HOMEPAGE: SlowResponse(b"")})
    monkeypatch.setattr(requests, "get", get)
    client = SetecSearchClient()
    with pytest.raises(FeedFetchError, match="PublicDiscoveryDeadlineExceeded"):
        client.resolve_config()
    assert client.budget.consumed_bytes == len(b"partial")


def test_asset_normalization_deduplicates_default_port_and_html_entities() -> None:
    body = (
        b'<script src="https://setec.mk:443/_next/static/a.js?x=1&amp;y=2"></script>'
        b'<script src="/_next/static/a.js?x=1&amp;y=2"></script>'
    )
    assert advertised_assets(body) == ("https://setec.mk/_next/static/a.js?x=1&y=2",)


def test_public_predicate_covers_latest_summary_bands_hits_and_id_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "get", small_get())
    root = StubResponse(count_payload([1499]))
    post = RecordingPost(
        [
            root,
            root,
            StubResponse(search_payload([price_payload("prod-1")])),
            root,
            StubResponse(search_payload([])),
            StubResponse(search_payload([])),
        ],
    )
    monkeypatch.setattr(requests, "post", post)
    catalog_client = SetecCatalogClient()
    assert (
        len(
            catalog_client.fetch_price_index(
                CATALOG_URL,
                retry_policy=no_wait_fetch_retry_policy(),
                is_shutdown_requested=lambda: False,
            ),
        )
        == 1
    )
    catalog_client.fetch_latest_products(CATALOG_URL)
    catalog_client.fetch_products_by_ids(
        CATALOG_URL,
        ["prod-1"],
        retry_policy=no_wait_fetch_retry_policy(),
        is_shutdown_requested=lambda: False,
    )
    assert len(post.bodies) == 6
    assert post.bodies[0] == post.bodies[3]
    for body in post.bodies:
        search_filter = body["filter"]
        if isinstance(search_filter, list):
            assert search_filter[0] == SETEC_PUBLIC_FILTER
        else:
            assert search_filter == SETEC_PUBLIC_FILTER
    assert post.bodies[5]["filter"] == [SETEC_PUBLIC_FILTER, 'id IN ["prod-1"]']


def test_public_filter_preserves_parenthesized_string_and_array_or_groups() -> None:
    assert public_search_body({"filter": "a = 1 OR b = 2"})["filter"] == (
        f"({SETEC_PUBLIC_FILTER}) AND (a = 1 OR b = 2)"
    )
    existing = [["a = 1", "b = 2"], "price >= 0"]
    assert public_search_body({"filter": existing})["filter"] == [
        SETEC_PUBLIC_FILTER,
        *existing,
    ]
    assert existing == [["a = 1", "b = 2"], "price >= 0"]


@pytest.mark.parametrize(
    "final",
    [
        count_payload([1499, 1499]),
        count_payload([1500]),
        b'{"hits":[],"facetDistribution":{}}',
        b'{"hits":[],"facetDistribution":{"status":{"published":1}},"facetStats":{}}',
    ],
)
def test_unstable_or_absent_root_recheck_does_not_write_price_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    final: bytes,
) -> None:
    monkeypatch.setattr(requests, "get", small_get())
    attempt = [
        StubResponse(count_payload([1499])),
        StubResponse(count_payload([1499])),
        StubResponse(search_payload([price_payload("prod-1")])),
        StubResponse(final),
    ]
    post = RecordingPost(attempt * 3)
    monkeypatch.setattr(requests, "post", post)
    database_path = tmp_path / "state.db"
    sender = RecordingSender([])
    with DeliveryStore(database_path) as store:
        # Include historical absent inventory and a current item with an old price.
        make_monitor(
            make_feed(),
            CatalogStub(
                [
                    (
                        make_product("prod-1", calculated_amount=100),
                        make_product("historical-now-absent", calculated_amount=200),
                    ),
                ],
            ),
            store,
            sender,
        ).scan()
        with sqlite3.connect(database_path) as connection:
            before = tuple(connection.iterdump())
        monitor = make_monitor(make_feed(), SetecCatalogClient(), store, sender)
        with pytest.raises(FeedFetchError):
            monitor.scan()
        with sqlite3.connect(database_path) as connection:
            assert tuple(connection.iterdump()) == before
    assert sender.messages == []


@pytest.mark.parametrize("count", [True, "1", 1.0, -1])
def test_native_counts_reject_coercion_and_negative_values(
    monkeypatch: pytest.MonkeyPatch,
    count: object,
) -> None:
    monkeypatch.setattr(requests, "get", small_get())
    payload = json.loads(count_payload([1499]))
    payload["facetDistribution"]["status"]["published"] = count
    post = RecordingPost([StubResponse(json.dumps(payload).encode())])
    monkeypatch.setattr(requests, "post", post)
    with pytest.raises(FeedFetchError, match="InvalidResponse"):
        SetecCatalogClient().fetch_price_index(
            CATALOG_URL,
            retry_policy=no_wait_fetch_retry_policy(),
            is_shutdown_requested=lambda: False,
        )
    assert len(post.urls) == 1


def test_native_next_inline_contract_with_encoded_routes_and_external_analytics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = [f"static/chunks/chunk-{index}.js" for index in range(35)]
    paths[30] = "static/chunks/app/%5Blocale%5D/%5B...slug%5D/page-placeholder.js"
    # Six absolute script paths are duplicated in the escaped inline Next list.
    # The other 29 paths exist only as explicitly advertised relative literals.
    scripts = "".join(f'<script src="/_next/{path}"></script>' for path in paths[:6])
    inline = json.dumps([1, "modules:" + json.dumps(paths)])
    html = (
        scripts
        + '<script src="https://analytics.example/analytics.js"></script>'
        + '<script src="https://cdn.example/_next/static/external.js"></script>'
        + f"<script>self.__next_f.push({inline})</script>"
    ).encode()
    urls = tuple("https://setec.mk/_next/" + path for path in paths)
    assert advertised_assets(html) == urls
    get = PublicGet(
        {
            HOMEPAGE: StubResponse(html),
            **{
                url: StubResponse(constructor() if index == 22 else b"/* unrelated */")
                for index, url in enumerate(urls)
            },
        },
    )
    monkeypatch.setattr(requests, "get", get)
    assert SetecSearchClient().resolve_config().key == KEY
    assert get.urls == [HOMEPAGE, *urls]


@pytest.mark.parametrize("suffix", ["", "/"])
def test_fixed_api_origin_accepts_only_optional_root_slash(suffix: str) -> None:
    content = constructor().replace(b"solslab.dev/", ("solslab.dev" + suffix).encode())
    assert extract_configs(content) == {KEY}


@pytest.mark.parametrize("suffix", ["/private", "/?query=1", "/#fragment", "//"])
def test_api_origin_does_not_accept_paths_queries_or_fragments(suffix: str) -> None:
    content = constructor().replace(b"solslab.dev/", ("solslab.dev" + suffix).encode())
    assert not extract_configs(content)


@pytest.mark.parametrize(
    "path",
    [
        "static/chunks/%2e%2e/secret.js",
        "static/chunks/%2Fsecret.js",
        "static/chunks/%5csecret.js",
        "static/chunks/%255Broute%255D/a.js",
        "static/chunks/%5Broute%5D/%2E%2E/a.js",
    ],
)
def test_relative_next_paths_reject_unsafe_encoding(path: str) -> None:
    with pytest.raises(FeedFetchError, match="InvalidPublicAsset"):
        static_url(path)
