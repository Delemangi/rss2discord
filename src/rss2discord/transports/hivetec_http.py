"""Secure, bounded HTTP retrieval for Hivetec product API pages."""

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlencode, urljoin, urlsplit

from pydantic import TypeAdapter, ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.retries import parse_retry_after
from rss2discord.transports.hivetec_bounds import (
    HIVETEC_DATES_API_PATH,
    HIVETEC_LABEL,
    HIVETEC_ORIGIN,
    HIVETEC_SHOP_PATH,
    HIVETEC_STORE_API_PATH,
    MAX_HIVETEC_REDIRECTS,
    HivetecPageRequest,
)
from rss2discord.transports.hivetec_budget import HivetecScanBudget
from rss2discord.transports.hivetec_models import HivetecProduct, HivetecProductDate
from rss2discord.transports.hivetec_transport import HivetecTransport

PRODUCT_FIELDS = "id,name,permalink,sku,prices,images,categories,is_in_stock"
DATE_FIELDS = "id,date_gmt,status"


@dataclass(frozen=True, slots=True)
class HivetecApiUrls:
    products: str
    dates: str


@dataclass(frozen=True, slots=True)
class FetchedHivetecProducts:
    products: tuple[HivetecProduct, ...]
    total: int
    total_pages: int


class HivetecHttpClient:
    """Retrieve Hivetec API responses without trusting redirects or sizes."""

    def __init__(self, budget: HivetecScanBudget) -> None:
        self._transport = HivetecTransport(budget)

    def build_api_urls(self, url: str) -> HivetecApiUrls:
        """Validate one exact HTTPS shop URL and derive credential-free APIs."""
        try:
            parsed = urlsplit(url)
            port = 443 if parsed.port is None else parsed.port
        except ValueError:
            raise FeedFetchError(HIVETEC_LABEL, "InvalidUrl") from None
        if (
            parsed.scheme != "https"
            or parsed.hostname != "hivetec.mk"
            or port != 443
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != HIVETEC_SHOP_PATH
            or parsed.query
            or parsed.fragment
            or url != f"{HIVETEC_ORIGIN}{HIVETEC_SHOP_PATH}"
        ):
            raise FeedFetchError(HIVETEC_LABEL, "InvalidUrl")
        return HivetecApiUrls(
            products=f"{HIVETEC_ORIGIN}{HIVETEC_STORE_API_PATH}",
            dates=f"{HIVETEC_ORIGIN}{HIVETEC_DATES_API_PATH}",
        )

    def fetch_products_page(
        self,
        api_url: str,
        request: HivetecPageRequest,
    ) -> FetchedHivetecProducts:
        query = urlencode(
            {
                "page": request.page,
                "per_page": request.per_page,
                "orderby": "date",
                "order": "desc",
                "_fields": PRODUCT_FIELDS,
            },
        )
        content, headers = self._fetch(
            f"{api_url}?{query}",
            request,
        )
        try:
            products = TypeAdapter(tuple[HivetecProduct, ...]).validate_json(content)
            total = int(headers["x-wp-total"])
            total_pages = int(headers["x-wp-totalpages"])
        except (KeyError, ValueError, ValidationError):
            raise FeedFetchError(HIVETEC_LABEL, "InvalidResponse") from None
        if total < 0 or total_pages < 0:
            raise FeedFetchError(HIVETEC_LABEL, "InvalidResponse")
        return FetchedHivetecProducts(products, total, total_pages)

    def fetch_product_dates(
        self,
        api_url: str,
        request: HivetecPageRequest,
    ) -> tuple[HivetecProductDate, ...]:
        query = urlencode(
            {
                "page": request.page,
                "per_page": request.per_page,
                "orderby": "date",
                "order": "desc",
                "_fields": DATE_FIELDS,
            },
        )
        content, _headers = self._fetch(
            f"{api_url}?{query}",
            request,
        )
        try:
            return TypeAdapter(tuple[HivetecProductDate, ...]).validate_json(content)
        except ValidationError:
            raise FeedFetchError(HIVETEC_LABEL, "InvalidResponse") from None

    def _fetch(
        self,
        url: str,
        request: HivetecPageRequest,
    ) -> tuple[bytes, Mapping[str, str]]:
        current_url = url
        for _ in range(MAX_HIVETEC_REDIRECTS + 1):
            response = self._transport.fetch(current_url, request)
            if response.url != current_url:
                raise FeedFetchError(HIVETEC_LABEL, "InvalidRedirect")
            if 300 <= response.status_code < 400:
                location = response.headers.get("location")
                if location is None:
                    raise FeedFetchError(HIVETEC_LABEL, "InvalidRedirect")
                current_url = _same_origin_redirect_url(current_url, location)
                continue
            if response.status_code >= 400:
                status_code = response.status_code
                raise FeedFetchError(
                    HIVETEC_LABEL,
                    "HTTPError",
                    status_code=status_code,
                    retryable=status_code in {408, 429} or 500 <= status_code < 600,
                    retry_after=parse_retry_after(
                        response.headers.get("retry-after"),
                    ),
                )
            return response.content, response.headers
        raise FeedFetchError(HIVETEC_LABEL, "TooManyRedirects")


def _same_origin_redirect_url(current_url: str, location: str) -> str:
    redirected_url = urljoin(current_url, location)
    try:
        redirected = urlsplit(redirected_url)
        port = 443 if redirected.port is None else redirected.port
    except ValueError:
        raise FeedFetchError(HIVETEC_LABEL, "InvalidRedirect") from None
    if (
        redirected.scheme != "https"
        or redirected.hostname != "hivetec.mk"
        or port != 443
        or redirected.username is not None
        or redirected.password is not None
    ):
        raise FeedFetchError(HIVETEC_LABEL, "InvalidRedirect")
    return redirected_url
