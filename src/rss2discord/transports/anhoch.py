"""Anhoch public catalog strategy."""

import math
from datetime import UTC, datetime
from typing import Annotated, Final
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import requests
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    ValidationError,
    field_validator,
)

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports.base import FeedFetchError, ScraperStrategy

ANHOCH_LABEL: Final = "Anhoch"
ANHOCH_PRODUCT_BASE_URL: Final = "https://www.anhoch.com/products/"
ANHOCH_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
ANHOCH_PRODUCTS_PER_PAGE: Final = 30
MAX_ANHOCH_PAGES: Final = 3
MAX_ANHOCH_RESPONSE_BYTES: Final = 1_048_576
ANHOCH_STREAM_CHUNK_BYTES: Final = 65_536
MAX_ANHOCH_REDIRECTS: Final = 10


class _AnhochPrice(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    formatted: Annotated[str, Field(min_length=1)]


class _AnhochImage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    path: Annotated[str, Field(min_length=1)]


class _AnhochInstallments(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    period: Annotated[int, Field(gt=0)]
    price: _AnhochPrice


class AnhochProduct(BaseModel):
    """Validated subset of one product from the public catalog API."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[int, Field(gt=0)]
    name: Annotated[str, Field(min_length=1)]
    slug: Annotated[str, Field(min_length=1)]
    price: _AnhochPrice
    selling_price: _AnhochPrice
    base_image: _AnhochImage | None = None
    is_in_stock: bool
    qty: int | None = None
    installments: _AnhochInstallments | None = None

    @field_validator("base_image", mode="before")
    @classmethod
    def normalize_empty_image(cls, value: JsonValue) -> JsonValue:
        if value == []:
            return None
        return value


class _AnhochProductPage(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    current_page: Annotated[int, Field(gt=0)]
    last_page: Annotated[int, Field(gt=0)]
    data: tuple[AnhochProduct, ...]


class _AnhochCatalogResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    products: _AnhochProductPage


class AnhochStrategy(ScraperStrategy):
    """Discover newly listed products from the public Anhoch catalog."""

    seed_existing_on_first_fetch = True

    def fetch_entries(self, url: str) -> tuple[list[AnhochProduct], str]:
        """Fetch a bounded window of the latest Anhoch products."""
        products: list[AnhochProduct] = []
        for page_number in range(1, MAX_ANHOCH_PAGES + 1):
            page = self._fetch_page(self._page_url(url, page_number))
            products.extend(page.data)
            if page.current_page >= page.last_page or not page.data:
                break
        return list(reversed(products)), ANHOCH_LABEL

    def get_entry_id(self, entry: AnhochProduct) -> EntryId:
        """Return the stable numeric Anhoch product ID."""
        return EntryId(str(entry.id))

    def get_entry_data(self, entry: AnhochProduct) -> EntryData:
        """Map a newly observed catalog product to Discord entry data."""
        metrics = [SourceMetric(label="Price", value=entry.selling_price.formatted)]
        if entry.price.formatted != entry.selling_price.formatted:
            metrics.append(SourceMetric(label="Original", value=entry.price.formatted))
        stock = str(entry.qty) if entry.is_in_stock and entry.qty is not None else "0"
        metrics.append(SourceMetric(label="Stock", value=stock))
        if entry.installments is not None:
            metrics.append(
                SourceMetric(
                    label="Installments",
                    value=(
                        f"{entry.installments.period} × "
                        f"{entry.installments.price.formatted}"
                    ),
                ),
            )
        return EntryData(
            title=entry.name,
            link=f"{ANHOCH_PRODUCT_BASE_URL}{entry.slug}",
            description="",
            author="",
            timestamp=datetime.now(UTC).isoformat(),
            image_url=entry.base_image.path if entry.base_image is not None else None,
            source_metrics=tuple(metrics),
        )

    @staticmethod
    def _page_url(url: str, page_number: int) -> str:
        try:
            parsed_url = urlsplit(url)
        except ValueError:
            raise FeedFetchError(ANHOCH_LABEL, "InvalidUrl") from None
        retained_query = [
            (key, value)
            for key, value in parse_qsl(parsed_url.query, keep_blank_values=True)
            if key not in {"sort", "perPage", "page"}
        ]
        retained_query.extend(
            (
                ("sort", "latest"),
                ("perPage", str(ANHOCH_PRODUCTS_PER_PAGE)),
                ("page", str(page_number)),
            ),
        )
        return urlunsplit(parsed_url._replace(query=urlencode(retained_query)))

    @staticmethod
    def _fetch_page(url: str) -> _AnhochProductPage:
        try:
            current_url = url
            for _ in range(MAX_ANHOCH_REDIRECTS + 1):
                with requests.get(
                    current_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": ANHOCH_USER_AGENT,
                        "X-Requested-With": "XMLHttpRequest",
                    },
                    timeout=30,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(
                                ANHOCH_LABEL,
                                "InvalidRedirect",
                            ) from None
                        _read_content(response)
                        current_url = urljoin(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        status_code = response.status_code
                        raise FeedFetchError(
                            ANHOCH_LABEL,
                            "HTTPError",
                            status_code=status_code,
                            retryable=(status_code == 429 or 500 <= status_code < 600),
                            retry_after=_parse_retry_after(
                                response.headers.get("Retry-After"),
                            ),
                        ) from None
                    content = _read_content(response)
                    break
            else:
                raise FeedFetchError(ANHOCH_LABEL, "TooManyRedirects") from None
        except ValueError:
            raise FeedFetchError(ANHOCH_LABEL, "InvalidUrl") from None
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                ANHOCH_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(ANHOCH_LABEL, type(error).__name__) from None

        try:
            return _AnhochCatalogResponse.model_validate_json(content).products
        except ValidationError:
            raise FeedFetchError(ANHOCH_LABEL, "InvalidResponse") from None


def _read_content(response: requests.Response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_ANHOCH_RESPONSE_BYTES:
            raise FeedFetchError(ANHOCH_LABEL, "ResponseTooLarge")

    content = bytearray()
    for chunk in response.iter_content(chunk_size=ANHOCH_STREAM_CHUNK_BYTES):
        if len(content) + len(chunk) > MAX_ANHOCH_RESPONSE_BYTES:
            raise FeedFetchError(ANHOCH_LABEL, "ResponseTooLarge")
        content.extend(chunk)
    return bytes(content)


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        return None
    return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None
