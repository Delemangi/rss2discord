from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field

import requests

INDEX_URL = "https://forum.it.mk/oglasnik/?token=secret-token"
FINAL_URL = "https://forum.it.mk/whats-new/oglasnik/5395366/"

COMPLETE_CARD = """
<div class="structItem structItem--listing" data-author="Hristijan121">
  <div class="structItem-cell structItem-cell--icon">
    <img src="/data/attachments/162/phone.jpg" alt="Iphone 12 64gb">
  </div>
  <div class="structItem-cell structItem-cell--main">
    <ul class="structItem-statuses">
      <li><i class="structItem-status structItem-status--locked" title="Заклучена"></i></li>
      <li><span class="ribbon ribbon--green">8.000 ден.</span></li>
    </ul>
    <div class="structItem-title">
      <span class="ribbon ribbon--blue">Купено</span>
      <a href="/oglasnik/iphone-12-64gb.6228/"> Iphone 12   64gb </a>
    </div>
    <div class="structItem-minor">
      <ul class="structItem-parts">
        <li><a href="/members/hristijan121.37878/" class="username">Hristijan121</a></li>
        <li class="structItem-startDate"><time datetime="2026-07-10T09:36:09+0200">10 јули 2026</time></li>
        <li><a href="/oglasnik/categories/mobilni-uredi-i-dodatoci.6/">Мобилни уреди и додатоци</a></li>
      </ul>
    </div>
    <div class="structItem-listingDescription">Telefonot e vo odlicna sostojba.</div>
  </div>
  <div class="structItem-cell structItem-cell--listingMeta">
    <dl><dt>Истекува</dt><dd><time datetime="2026-09-10T09:36:09+0200">10 септември 2026</time></dd></dl>
    <dl><dt>Тип</dt><dd>Продавам</dd></dl>
    <dl><dt>Состојба</dt><dd>Користен одлично сочуван</dd></dl>
    <dl><dt>Прегледи</dt><dd>2.817</dd></dl>
  </div>
</div>
"""

NEWER_CARD = """
<div class="structItem structItem--listing" data-author="Seller">
  <div class="structItem-cell structItem-cell--icon">
    <img src="https://forum.it.mk/images/no-product-image.png" alt="GPU">
  </div>
  <div class="structItem-cell structItem-cell--main">
    <div class="structItem-title"><a href="/oglasnik/gpu.6271/">GPU</a></div>
    <div class="structItem-listingDescription">Newer listing</div>
  </div>
</div>
"""


@dataclass(frozen=True, slots=True)
class StubResponse:
    text: str
    status_code: int
    url: str = FINAL_URL
    headers: Mapping[str, str] = field(default_factory=dict)
    chunks: tuple[bytes, ...] = ()

    @property
    def is_redirect(self) -> bool:
        return 300 <= self.status_code < 400 and "Location" in self.headers

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        del chunk_size
        yield from self.chunks or (self.text.encode(),)


@dataclass(frozen=True, slots=True)
class StubGet:
    response: StubResponse

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        allow_redirects: bool,
        stream: bool = False,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, allow_redirects, stream
        return nullcontext(self.response)


@dataclass(frozen=True, slots=True)
class RaisingGet:
    error: requests.RequestException

    def __call__(
        self,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: int,
        allow_redirects: bool,
        stream: bool = False,
    ) -> AbstractContextManager[StubResponse]:
        del url, headers, timeout, allow_redirects, stream
        raise self.error


def make_response(html: str, status_code: int = 200) -> StubResponse:
    return StubResponse(text=html, status_code=status_code)
