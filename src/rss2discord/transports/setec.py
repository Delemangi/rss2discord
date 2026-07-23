"""Setec public catalog strategy."""

import math
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, ClassVar, Final, Literal, final, override
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports.base import FeedFetchError, ScraperStrategy

SETEC_LABEL: Final = "Setec"
SETEC_API_PATH: Final = "/api/medusa/products/list"
SETEC_PRODUCT_BASE_URL: Final = "https://setec.mk/products/"
SETEC_REGION_ID: Final = "mk"
SETEC_USER_AGENT: Final = "rss2discord/0.1 (+https://github.com/Delemangi/rss2discord)"
SETEC_WINDOW_SIZE: Final = 30
MAX_SETEC_RESPONSE_BYTES: Final = 1_048_576
SETEC_STREAM_CHUNK_BYTES: Final = 65_536
MAX_SETEC_REDIRECTS: Final = 10


class _SetecCalculatedPrice(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_amount: Annotated[int, Field(ge=0)]
    original_amount: Annotated[int, Field(ge=0)]
    currency_code: Literal["mkd"]


class _SetecVariant(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    calculated_price: _SetecCalculatedPrice


class _SetecCategory(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    name: Annotated[str, Field(min_length=1)]


class SetecProduct(BaseModel):
    """Validated subset of one product from the Setec catalog API."""

    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    id: Annotated[str, Field(min_length=1)]
    title: Annotated[str, Field(min_length=1)]
    handle: Annotated[str, Field(min_length=1)]
    thumbnail: str | None = None
    created_at: datetime
    variants: tuple[_SetecVariant, ...]
    categories: tuple[_SetecCategory, ...] = ()


class _SetecCatalogResponse(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True)

    count: Annotated[int, Field(ge=0)]
    products: tuple[SetecProduct, ...]


@final
class SetecStrategy(ScraperStrategy):
    """Discover newly listed products from the public Setec catalog."""

    seed_existing_on_first_fetch: bool = True

    @override
    def fetch_entries(self, url: str) -> tuple[list[SetecProduct], str]:
        """Fetch the latest window of Setec products via the medusa API."""
        api_url = _build_api_url(url)
        probe = self._fetch(api_url, limit=1, offset=0)
        count = probe.count
        if count == 0:
            return [], SETEC_LABEL
        if count <= 1:
            return list(probe.products), SETEC_LABEL
        offset = max(count - SETEC_WINDOW_SIZE, 0)
        window = self._fetch(api_url, limit=SETEC_WINDOW_SIZE, offset=offset)
        return list(window.products), SETEC_LABEL

    @override
    def get_entry_id(self, entry: SetecProduct) -> EntryId:
        """Return the stable Setec product ID."""
        return EntryId(entry.id)

    @override
    def get_entry_data(self, entry: SetecProduct) -> EntryData:
        """Map a catalog product to Discord entry data."""
        price_variant = entry.variants[0].calculated_price if entry.variants else None
        metrics: list[SourceMetric] = []
        if price_variant is not None:
            metrics.append(
                SourceMetric(
                    label="Price",
                    value=_format_mkd(price_variant.calculated_amount),
                ),
            )
            if price_variant.original_amount != price_variant.calculated_amount:
                metrics.append(
                    SourceMetric(
                        label="Original",
                        value=_format_mkd(price_variant.original_amount),
                    ),
                )
        return EntryData(
            title=entry.title,
            link=urljoin(SETEC_PRODUCT_BASE_URL, entry.handle),
            description="",
            author="",
            timestamp=entry.created_at.isoformat(),
            image_url=entry.thumbnail or None,
            categories=tuple(cat.name for cat in entry.categories),
            source_metrics=tuple(metrics),
        )

    @staticmethod
    def _fetch(api_url: str, *, limit: int, offset: int) -> _SetecCatalogResponse:
        query = urlencode(
            {"limit": limit, "offset": offset, "region_id": SETEC_REGION_ID},
        )
        full_url = f"{api_url}?{query}"
        try:
            current_url = full_url
            for _ in range(MAX_SETEC_REDIRECTS + 1):
                with requests.get(
                    current_url,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": SETEC_USER_AGENT,
                    },
                    timeout=30,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    if 300 <= response.status_code < 400:
                        location = response.headers.get("Location")
                        if location is None:
                            raise FeedFetchError(
                                SETEC_LABEL,
                                "InvalidRedirect",
                            ) from None
                        _ = _read_content(response)
                        current_url = urljoin(current_url, location)
                        continue
                    try:
                        response.raise_for_status()
                    except requests.HTTPError:
                        status_code = response.status_code
                        raise FeedFetchError(
                            SETEC_LABEL,
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
                raise FeedFetchError(SETEC_LABEL, "TooManyRedirects") from None
        except ValueError:
            raise FeedFetchError(SETEC_LABEL, "InvalidUrl") from None
        except (requests.ConnectionError, requests.Timeout) as error:
            raise FeedFetchError(
                SETEC_LABEL,
                type(error).__name__,
                retryable=True,
            ) from None
        except requests.RequestException as error:
            raise FeedFetchError(SETEC_LABEL, type(error).__name__) from None

        try:
            return _SetecCatalogResponse.model_validate_json(content)
        except ValidationError:
            raise FeedFetchError(SETEC_LABEL, "InvalidResponse") from None


def _build_api_url(url: str) -> str:
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
    return urlunsplit(parsed._replace(path=SETEC_API_PATH, query="", fragment=""))


def _read_content(response: requests.Response) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            declared_bytes = int(content_length)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > MAX_SETEC_RESPONSE_BYTES:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")

    content = bytearray()
    chunks: Iterator[bytes] = response.iter_content(
        chunk_size=SETEC_STREAM_CHUNK_BYTES,
    )
    for chunk in chunks:
        if len(content) + len(chunk) > MAX_SETEC_RESPONSE_BYTES:
            raise FeedFetchError(SETEC_LABEL, "ResponseTooLarge")
        content.extend(chunk)
    return bytes(content)


def _format_mkd(amount: int) -> str:
    """Format an integer MKD amount with dot thousands separator, e.g. 1499 → '1.499 ден.'"""
    formatted = f"{amount:,}".replace(",", ".")
    return f"{formatted} ден."


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        retry_after = float(value)
    except ValueError:
        return None
    return retry_after if math.isfinite(retry_after) and retry_after >= 0 else None
