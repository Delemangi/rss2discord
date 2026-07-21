from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass

import pytest
import requests

from rss2discord.transports.itmk_oglasnik_http import fetch_itmk_oglasnik_page

START_URL = "https://forum.it.mk/redirect-start"
FINAL_URL = "https://forum.it.mk/redirect-final"
LISTING_HTML = b"""
<h1>Oglasnik</h1>
<div class="structItem" data-author="Seller">
  <div class="structItem-title">
    <a href="/oglasnik/redirected-listing.9001/">Redirected listing</a>
  </div>
  <div class="structItem-listingDescription">Redirected summary</div>
</div>
"""


@dataclass(frozen=True, slots=True)
class RedirectResponse:
    body: bytes
    status_code: int
    url: str
    headers: Mapping[str, str]

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400 and "Location" in self.headers

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield self.body


class SequenceGet:
    """Return successive responses while recording the redirect policy."""

    def __init__(self, responses: tuple[RedirectResponse, ...]) -> None:
        self._responses = iter(responses)

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        allow_redirects: bool,
        stream: bool,
    ) -> AbstractContextManager[RedirectResponse]:
        del url, headers, timeout, stream
        assert allow_redirects is False
        return nullcontext(next(self._responses))


def test_itmk_oglasnik_strategy_bounds_redirects_before_following(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    responses = (
        RedirectResponse(
            body=b"redirect",
            status_code=302,
            url=START_URL,
            headers={"Location": FINAL_URL},
        ),
        RedirectResponse(
            body=LISTING_HTML,
            status_code=200,
            url=FINAL_URL,
            headers={},
        ),
    )
    monkeypatch.setattr(requests, "get", SequenceGet(responses))

    # When
    content, final_url = fetch_itmk_oglasnik_page(START_URL)

    # Then
    assert content == LISTING_HTML
    assert final_url == FINAL_URL
