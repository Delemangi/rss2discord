"""Exercise Reddit OAuth, pagination parameters, caching, and error mapping."""

import json
import logging

import pytest
import requests

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.reddit.scope import RedditScope
from tests.reddit_helpers import (
    REDDIT_SCOPE,
    FakeResponse,
    FakeSession,
    StepClock,
    fixture_bytes,
    make_reddit_client,
    set_reddit_environment,
)


def token_body(access_token: str, expires_in: int = 86_400) -> bytes:
    return json.dumps(
        {
            "access_token": access_token,
            "token_type": "bearer",
            "expires_in": expires_in,
        },
    ).encode()


def listing_page(requested: int) -> FakeResponse:
    return FakeResponse(requested, fixture_bytes("listing_empty.json"))


def test_token_request_uses_basic_auth_grant_and_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch, user_agent="rss2discord-test/1.0")
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            listing_page(200),
        ],
    )
    client = make_reddit_client(session)

    # When
    page = client.fetch_listing(RedditScope("python", "top", "week"))

    # Then
    assert page.posts == ()
    assert page.after is None
    method, url, token_kwargs = session.calls[0]
    assert method == "POST"
    assert url == "https://www.reddit.com/api/v1/access_token"
    assert token_kwargs["auth"] == ("fixture-id", "fixture-secret")
    assert token_kwargs["data"] == {"grant_type": "client_credentials"}
    assert token_kwargs["headers"] == {"User-Agent": "rss2discord-test/1.0"}

    method, url, listing_kwargs = session.calls[1]
    assert method == "GET"
    assert url == "https://oauth.reddit.com/r/python/top"
    assert listing_kwargs["headers"] == {
        "Authorization": "bearer fixture-token",
        "User-Agent": "rss2discord-test/1.0",
    }
    assert listing_kwargs["params"] == {
        "limit": 100,
        "raw_json": 1,
        "t": "week",
    }


def test_listing_passes_after_and_count_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            listing_page(200),
        ],
    )
    client = make_reddit_client(session)

    # When
    client.fetch_listing(REDDIT_SCOPE, after="t3_cursor", count=42)

    # Then
    assert session.calls[1][2]["params"] == {
        "limit": 100,
        "raw_json": 1,
        "after": "t3_cursor",
        "count": 42,
    }


def test_token_is_cached_until_its_safety_adjusted_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    clock = StepClock()
    session = FakeSession(
        [
            FakeResponse(200, token_body("token-a", expires_in=120)),
            listing_page(200),
            listing_page(200),
            FakeResponse(200, token_body("token-b")),
            listing_page(200),
        ],
    )
    client = make_reddit_client(session, clock=clock)

    # When
    client.fetch_listing(REDDIT_SCOPE)
    client.fetch_listing(REDDIT_SCOPE)
    clock.advance(61)
    client.fetch_listing(REDDIT_SCOPE)

    # Then
    token_requests = [call for call in session.calls if call[0] == "POST"]
    assert len(token_requests) == 2
    assert session.calls[2][2]["headers"]["Authorization"] == "bearer token-a"
    assert session.calls[4][2]["headers"]["Authorization"] == "bearer token-b"


def test_401_refreshes_token_once_and_retries_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, token_body("token-a")),
            FakeResponse(401, fixture_bytes("error_401.json")),
            FakeResponse(200, token_body("token-b")),
            FakeResponse(200, fixture_bytes("listing_page_1.json")),
        ],
    )
    client = make_reddit_client(session)

    # When
    page = client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert len(page.posts) == 6
    assert [call[0] for call in session.calls] == ["POST", "GET", "POST", "GET"]
    assert session.calls[1][2]["headers"]["Authorization"] == "bearer token-a"
    assert session.calls[3][2]["headers"]["Authorization"] == "bearer token-b"


def test_second_401_is_non_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, token_body("token-a")),
            FakeResponse(401, fixture_bytes("error_401.json")),
            FakeResponse(200, token_body("token-b")),
            FakeResponse(401, fixture_bytes("error_401.json")),
        ],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.status_code == 401
    assert error.value.retryable is False
    assert [call[0] for call in session.calls] == ["POST", "GET", "POST", "GET"]


def test_429_uses_retry_after_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            FakeResponse(
                429,
                fixture_bytes("error_429.json"),
                {"Retry-After": "12"},
            ),
        ],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.status_code == 429
    assert error.value.retryable is True
    assert error.value.retry_after == 12.0


def test_429_uses_ratelimit_reset_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            FakeResponse(
                429,
                fixture_bytes("error_429.json"),
                {"X-Ratelimit-Reset": "30"},
            ),
        ],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.retryable is True
    assert error.value.retry_after == 30.0


def test_500_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            FakeResponse(500, fixture_bytes("error_500.json")),
        ],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.status_code == 500
    assert error.value.retryable is True


def test_connection_error_is_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            requests.ConnectionError("connection reset"),
        ],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.cause_type == "ConnectionError"
    assert error.value.retryable is True


def test_403_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, fixture_bytes("token_success.json")),
            FakeResponse(403, fixture_bytes("error_403.json")),
        ],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.status_code == 403
    assert error.value.retryable is False


def test_token_endpoint_401_is_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [FakeResponse(401, fixture_bytes("token_error.json"))],
    )
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.status_code == 401
    assert error.value.retryable is False
    assert [call[0] for call in session.calls] == ["POST"]


def test_oversize_body_fails_with_response_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    set_reddit_environment(monkeypatch)
    session = FakeSession(
        [
            FakeResponse(200, token_body("token-a")),
            FakeResponse(200, b"x" * 1_000),
        ],
    )
    client = make_reddit_client(session, max_response_bytes=200)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.cause_type == "ResponseTooLarge"
    assert error.value.retryable is False


def test_missing_credentials_fails_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.delenv("REDDIT_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("REDDIT_USER_AGENT", raising=False)
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    session = FakeSession([])
    client = make_reddit_client(session)

    # When
    with pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    assert error.value.cause_type == "MissingCredentials"
    assert error.value.retryable is False
    assert session.calls == []


def test_error_text_never_contains_credentials_or_tokens(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given
    monkeypatch.setenv("REDDIT_CLIENT_ID", "fixture-id")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "fixture-secret-value")
    monkeypatch.setenv("REDDIT_USER_AGENT", "rss2discord-test/1.0")
    monkeypatch.delenv("REDDIT_ACCESS_TOKEN", raising=False)
    session = FakeSession(
        [
            FakeResponse(200, token_body("token-a")),
            FakeResponse(401, fixture_bytes("error_401.json")),
            FakeResponse(200, token_body("token-b")),
            FakeResponse(401, fixture_bytes("error_401.json")),
        ],
    )
    client = make_reddit_client(session)

    # When
    with caplog.at_level(logging.DEBUG), pytest.raises(FeedFetchError) as error:
        client.fetch_listing(REDDIT_SCOPE)

    # Then
    combined = f"{error.value!s}{error.value!r}{caplog.text}"
    for secret in ("fixture-secret-value", "fixture-id", "token-a", "token-b"):
        assert secret not in combined
