from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

FeedIdValue = Annotated[
    str,
    Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
NonEmptyString = Annotated[str, Field(min_length=1)]
WebhookName = Annotated[str, Field(min_length=1, max_length=80)]


class FeedConfig(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    id: FeedIdValue
    url: NonEmptyString
    webhook: NonEmptyString
    name: str | None = None
    strategy: Literal["rss", "xenforo"] = "rss"
    webhook_name: WebhookName | None = None
    webhook_avatar: str | None = None
    embed_color: Annotated[int, Field(ge=0, le=0xFFFFFF)] | None = None


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    refresh_interval: Annotated[float, Field(gt=0)] = 300
    delay_between_feeds: Annotated[float, Field(ge=0)] = 0
    delay_between_posts: Annotated[float, Field(ge=0)] = 2
    max_post_age_days: Annotated[int, Field(ge=0)] = 7
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
