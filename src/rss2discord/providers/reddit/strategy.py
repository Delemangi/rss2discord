"""Reddit subreddit discovery over a listing client."""

from datetime import UTC, datetime
from html import unescape
from typing import Final, Protocol, final, override
from urllib.parse import urlsplit

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.reddit.bounds import (
    MAX_REDDIT_PAGES,
    MAX_REDDIT_POSTS,
    REDDIT_DISCUSSION_ORIGIN,
)
from rss2discord.providers.reddit.client import RedditClient
from rss2discord.providers.reddit.models import RedditPage, RedditPost
from rss2discord.providers.reddit.scope import RedditScope, parse_reddit_scope
from rss2discord.transports.base import ScraperStrategy
from rss2discord.url_normalization import normalize_remote_media_url

THUMBNAIL_SENTINELS: Final = frozenset({"self", "default", "nsfw", "spoiler"})
REMOVED_TITLE_SENTINELS: Final = frozenset(
    {"[removedbymoderator]", "[removedbyreddit]", "[deleted]"},
)
REMOVED_BODY_SENTINELS: Final = frozenset({"[removed]", "[deleted]"})
REMOVED_AUTHOR_SENTINELS: Final = frozenset({"[deleted]", "[removed]"})
DIRECT_IMAGE_HOSTS: Final = frozenset({"i.redd.it", "preview.redd.it"})


class RedditListingClient(Protocol):
    """The listing surface the strategy depends on."""

    def fetch_listing(
        self,
        scope: RedditScope,
        *,
        after: str | None = None,
        count: int = 0,
    ) -> RedditPage: ...


@final
class RedditStrategy(ScraperStrategy):
    """Read up to three bounded pages from one subreddit listing."""

    # A newly configured subreddit must baseline its current listing instead of
    # dumping up to 300 existing posts. An empty first fetch must not mark the
    # feed initialized, so a later fetch still seeds cleanly.
    seed_existing_on_first_fetch = True
    require_entries_for_initialization = True

    def __init__(
        self,
        client: RedditListingClient | None = None,
        *,
        show_engagement: bool = True,
        filter_removed: bool = False,
    ) -> None:
        self._client = client if client is not None else RedditClient()
        self._show_engagement = show_engagement
        self._filter_removed = filter_removed

    @override
    def fetch_entries(self, url: str) -> tuple[list[RedditPost], str]:
        scope = parse_reddit_scope(url)
        posts: list[RedditPost] = []
        seen_cursors: set[str] = set()
        seen_entries: set[str] = set()
        cursor: str | None = None
        pages = 0
        while pages < MAX_REDDIT_PAGES and len(posts) < MAX_REDDIT_POSTS:
            page = self._client.fetch_listing(
                scope,
                after=cursor,
                count=len(posts),
            )
            pages += 1
            for post in page.posts:
                if _is_excluded(post):
                    continue
                if self._filter_removed and _is_removed(post):
                    continue
                entry_key = post.name or post.id or post.permalink
                if entry_key is not None:
                    if entry_key in seen_entries:
                        continue
                    seen_entries.add(entry_key)
                posts.append(post)
            if len(posts) > MAX_REDDIT_POSTS:
                del posts[MAX_REDDIT_POSTS:]
            if page.after is None or page.after in seen_cursors:
                break
            seen_cursors.add(page.after)
            cursor = page.after
        # Pages and their children arrive newest-first; reverse the collected
        # range so delivery follows the RSS strategy's oldest-first order.
        return list(reversed(posts)), f"r/{scope.subreddit}"

    @override
    def get_entry_id(self, entry: RedditPost) -> EntryId | None:
        if not entry.name:
            return None
        return EntryId(entry.name)

    @override
    def get_entry_data(self, entry: RedditPost) -> EntryData:
        discussion = _discussion_url(entry)
        image_urls = _image_urls(entry)
        is_image = entry.is_gallery or entry.post_hint == "image"
        outbound = entry.url_overridden_by_dest or entry.url
        if entry.is_self or is_image:
            link = discussion or outbound or ""
            discussion_url = None
        else:
            link = outbound or discussion or ""
            discussion_url = discussion
        selftext = entry.selftext if entry.is_self else None
        return EntryData(
            title=entry.title or "",
            link=link,
            description=self._truncate(self._clean_html(selftext or "")),
            author=entry.author or "",
            timestamp=_timestamp(entry.created_utc),
            discussion_url=discussion_url,
            image_url=image_urls[0] if image_urls else None,
            image_urls=image_urls,
            categories=(entry.link_flair_text,) if entry.link_flair_text else (),
            source_metrics=_metrics(
                entry,
                include_engagement=self._show_engagement,
            ),
        )


def _is_excluded(post: RedditPost) -> bool:
    return post.stickied or post.pinned or post.over_18 or post.spoiler


def _is_removed(post: RedditPost) -> bool:
    """Reject archived posts whose title, body, or author marks them removed."""
    title = "".join((post.title or "").split()).casefold()
    if title in REMOVED_TITLE_SENTINELS:
        return True
    permalink = (post.permalink or "").rstrip("/").casefold()
    if permalink.endswith("/removed_by_moderator"):
        return True
    if post.is_self:
        body = "".join((post.selftext or "").split()).casefold()
        if body in REMOVED_BODY_SENTINELS:
            return True
    author = "".join((post.author or "").split()).casefold()
    return author in REMOVED_AUTHOR_SENTINELS


def _discussion_url(post: RedditPost) -> str | None:
    if not post.permalink:
        return None
    return f"{REDDIT_DISCUSSION_ORIGIN}{post.permalink}"


def _timestamp(created_utc: float | None) -> str | None:
    if created_utc is None:
        return None
    try:
        return datetime.fromtimestamp(created_utc, tz=UTC).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def _metrics(
    post: RedditPost,
    *,
    include_engagement: bool = True,
) -> tuple[SourceMetric, ...]:
    metrics: list[SourceMetric] = []
    if include_engagement and post.score is not None:
        metrics.append(SourceMetric("Score", str(post.score)))
    if include_engagement and post.num_comments is not None:
        metrics.append(SourceMetric("Comments", str(post.num_comments)))
    if post.domain and not post.domain.casefold().startswith("self."):
        metrics.append(SourceMetric("Domain", post.domain))
    return tuple(metrics)


def _image_urls(post: RedditPost) -> tuple[str, ...]:
    if post.is_gallery:
        return _gallery_image_urls(post)
    if post.post_hint != "image":
        return ()
    image_url = (
        _preview_image_url(post)
        or _thumbnail_image_url(post.thumbnail)
        or _direct_image_url(post)
    )
    return (image_url,) if image_url is not None else ()


def _preview_image_url(post: RedditPost) -> str | None:
    preview = post.preview
    if preview is None or not preview.images:
        return None
    source = preview.images[0].source
    if source is None:
        return None
    return _safe_image_url(source.url)


def _thumbnail_image_url(thumbnail: str | None) -> str | None:
    if thumbnail is None or thumbnail.casefold() in THUMBNAIL_SENTINELS:
        return None
    return _safe_image_url(thumbnail)


def _direct_image_url(post: RedditPost) -> str | None:
    """Recover an image URL when the archive omitted the preview object."""
    for value in (post.url_overridden_by_dest, post.url):
        image_url = _safe_image_url(value)
        if image_url is None:
            continue
        if urlsplit(image_url).hostname in DIRECT_IMAGE_HOSTS:
            return image_url
    return None


def _gallery_image_urls(post: RedditPost) -> tuple[str, ...]:
    gallery = post.gallery_data
    metadata = post.media_metadata
    if gallery is None or metadata is None:
        return ()
    urls: list[str] = []
    for item in gallery.items:
        if not item.media_id:
            continue
        media = metadata.get(item.media_id)
        if media is None or media.s is None:
            continue
        image_url = _safe_image_url(media.s.u)
        if image_url is not None:
            urls.append(image_url)
    return tuple(urls)


def _safe_image_url(value: str | None) -> str | None:
    if value is None:
        return None
    return normalize_remote_media_url(unescape(value))
