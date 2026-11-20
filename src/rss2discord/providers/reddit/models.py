"""Lenient Pydantic models for Reddit OAuth and listing payloads."""

from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class _RedditModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore")


class RedditToken(_RedditModel):
    """OAuth client-credentials response."""

    access_token: str
    token_type: str | None = None
    expires_in: int | None = None
    scope: str | None = None


class RedditImageSource(_RedditModel):
    url: str | None = None
    width: int | None = None
    height: int | None = None


class RedditPreviewImage(_RedditModel):
    source: RedditImageSource | None = None


class RedditPreview(_RedditModel):
    images: list[RedditPreviewImage] = Field(default_factory=list)


class RedditGalleryItem(_RedditModel):
    media_id: str | None = None


class RedditGalleryData(_RedditModel):
    items: list[RedditGalleryItem] = Field(default_factory=list)


class RedditMediaSource(_RedditModel):
    u: str | None = None
    width: int | None = None
    height: int | None = None


class RedditMedia(_RedditModel):
    status: str | None = None
    s: RedditMediaSource | None = None


class RedditPost(_RedditModel):
    """One link, self, image, or gallery post from a listing."""

    name: str | None = None
    id: str | None = None
    title: str | None = None
    author: str | None = None
    created_utc: float | None = None
    selftext: str | None = None
    url: str | None = None
    url_overridden_by_dest: str | None = None
    permalink: str | None = None
    post_hint: str | None = None
    is_self: bool = False
    is_gallery: bool = False
    over_18: bool = False
    spoiler: bool = False
    stickied: bool = False
    pinned: bool = False
    link_flair_text: str | None = None
    score: int | None = None
    num_comments: int | None = None
    domain: str | None = None
    thumbnail: str | None = None
    preview: RedditPreview | None = None
    gallery_data: RedditGalleryData | None = None
    media_metadata: dict[str, RedditMedia] | None = None


class RedditChild(_RedditModel):
    """One listing child; malformed children are skipped individually."""

    kind: str | None = None
    data: RedditPost | None = None


class RedditListingData(_RedditModel):
    after: str | None = None
    before: str | None = None
    # Children stay arbitrary here so one malformed post cannot invalidate
    # the whole listing envelope.
    children: list[Any] = Field(default_factory=list)


class RedditListing(_RedditModel):
    """The listing envelope returned by the subreddit endpoint."""

    kind: str | None = None
    data: RedditListingData


@dataclass(frozen=True, slots=True)
class RedditPage:
    """One parsed listing page in upstream newest-first order."""

    posts: tuple[RedditPost, ...]
    after: str | None
