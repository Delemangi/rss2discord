from decimal import Decimal
from pathlib import Path
from typing import Annotated, ClassVar, Final, Literal, Self
from urllib.parse import quote

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rss2discord.defaults import DEFAULT_PRICE_INLINE_LIMIT
from rss2discord.provider_metadata import PRICE_STRATEGIES, RESTOCK_STRATEGIES
from rss2discord.url_normalization import (
    MAX_HTTP_URL_LENGTH,
    normalize_remote_media_url,
)

FeedIdValue = Annotated[
    str,
    Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
NonEmptyString = Annotated[str, Field(min_length=1)]
WebhookName = Annotated[str, Field(min_length=1, max_length=80)]
FeedAdapterName = Literal["hackernews", "reddit"]
FeedStrategyName = Literal[
    "rss",
    "xenforo",
    "itmk_oglasnik",
    "ananas",
    "anhoch",
    "ddstore",
    "gjirafa50",
    "hivetec",
    "neksio",
    "neptun",
    "pazar3",
    "proxnet",
    "reklama5",
    "reddit",
    "arctic_shift",
    "setec",
    "cccenter",
    "technomarket",
    "time_mk",
    "fakulteti_mk",
    "emsc",
    "tweets",
    "wolfinet",
    "woocommerce",
]
MIN_GJIRAFA50_PRICE_CHECK_INTERVAL: Final = 21_600
# Providers whose price snapshots report stock, so they can alert on restocks.
# Derived from StrategyMetadata.restock in provider_metadata.


class FeedConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    id: FeedIdValue
    url: NonEmptyString
    webhook: NonEmptyString
    name: str | None = None
    strategy: FeedStrategyName = "rss"
    seed_existing_on_first_fetch: bool = False
    adapter: FeedAdapterName | None = None
    webhook_name: WebhookName | None = None
    webhook_avatar: str | None = None
    embed_color: Annotated[int, Field(ge=0, le=0xFFFFFF)] | None = None
    ordinary_check_interval: (
        Annotated[
            float,
            Field(gt=0, allow_inf_nan=False),
        ]
        | None
    ) = None
    price_check_interval: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = (
        None
    )
    price_inline_limit: Annotated[int, Field(ge=1, le=1000)] = (
        DEFAULT_PRICE_INLINE_LIMIT
    )
    price_min_change_percentage: Annotated[
        float,
        Field(ge=0, allow_inf_nan=False),
    ] = 0
    price_min_change_amount: Annotated[Decimal, Field(ge=0)] | None = None
    price_ignore_increases: bool = False
    restock_alerts: bool = False
    digest_threshold: Annotated[int, Field(ge=1)] | None = 100
    stale_after_days: Annotated[int, Field(ge=1)] | None = None

    @field_validator("webhook_avatar")
    @classmethod
    def require_safe_webhook_avatar(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = normalize_remote_media_url(value)
        if normalized is None:
            msg = "webhook_avatar must be a safe HTTPS URL"
            raise ValueError(msg)
        encoded = quote(normalized, safe=":/?#[]@!$&'*+,;=%-._~")
        if len(encoded) > MAX_HTTP_URL_LENGTH:
            msg = "webhook_avatar must not exceed 2048 encoded characters"
            raise ValueError(msg)
        return encoded

    @model_validator(mode="after")
    def validate_strategy_options(self) -> Self:
        if self.adapter is not None and self.strategy != "rss":
            msg = "feed adapters require the rss strategy"
            raise ValueError(msg)
        if self.price_check_interval is not None:
            if (
                self.strategy == "gjirafa50"
                and self.price_check_interval < MIN_GJIRAFA50_PRICE_CHECK_INTERVAL
            ):
                msg = "gjirafa50 price_check_interval must be at least 21600 seconds"
                raise ValueError(msg)
            if self.strategy not in PRICE_STRATEGIES:
                supported = ", ".join(sorted(PRICE_STRATEGIES))
                msg = f"price_check_interval requires a price strategy: {supported}"
                raise ValueError(msg)
        if (
            self.price_inline_limit != DEFAULT_PRICE_INLINE_LIMIT
            and self.strategy not in PRICE_STRATEGIES
        ):
            supported = ", ".join(sorted(PRICE_STRATEGIES))
            msg = f"price_inline_limit requires a price strategy: {supported}"
            raise ValueError(msg)
        for field_name, enabled in (
            (
                "price_min_change_percentage",
                self.price_min_change_percentage != 0,
            ),
            ("price_min_change_amount", self.price_min_change_amount is not None),
            ("price_ignore_increases", self.price_ignore_increases),
        ):
            if enabled and self.strategy not in PRICE_STRATEGIES:
                supported = ", ".join(sorted(PRICE_STRATEGIES))
                msg = f"{field_name} requires a price strategy: {supported}"
                raise ValueError(msg)
        if self.restock_alerts and self.strategy not in RESTOCK_STRATEGIES:
            supported = ", ".join(sorted(RESTOCK_STRATEGIES))
            msg = f"restock_alerts requires a stock-aware price strategy: {supported}"
            raise ValueError(msg)
        return self


class AppConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )

    refresh_interval: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 300
    delay_between_feeds: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 0
    delay_between_posts: Annotated[float, Field(ge=0, allow_inf_nan=False)] = 2
    max_post_age_days: Annotated[int, Field(ge=0)] = 7
    state_retention_days: Annotated[int, Field(ge=0)] = 90
    health_webhook: NonEmptyString | None = None
    health_summary_interval: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 86_400
    feeds: tuple[FeedConfig, ...] = ()

    @model_validator(mode="after")
    def require_unique_feed_ids(self) -> Self:
        feed_ids = [feed.id for feed in self.feeds]
        if len(feed_ids) != len(set(feed_ids)):
            msg = "feed IDs must be unique"
            raise ValueError(msg)
        return self


def load_config(path: Path) -> AppConfig:
    with path.open(encoding="utf-8") as config_file:
        return AppConfig.model_validate(yaml.safe_load(config_file))
