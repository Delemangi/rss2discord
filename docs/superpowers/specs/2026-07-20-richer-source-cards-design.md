# Richer Source Cards Design

## Context

The production deployment currently monitors eleven Reddit Atom feeds, Hacker News RSS, and one Kajgana XenForo thread. All sources are normalized to title, link, description, author, timestamp, and source title before rendering. This hides useful structured RSS information, especially Hacker News discussion links, and prevents Components v2 from using optional thumbnails.

The design must remain safe for non-application-owned Discord webhooks, preserve the existing configuration and delivery ledger, and degrade cleanly when optional feed fields are absent or invalid.

## Goals

- Give Reddit, Hacker News, generic RSS, and XenForo messages a clear source identity.
- Preserve a distinct discussion URL when an RSS entry provides one.
- Show up to three categories and one structured thumbnail when available.
- Keep sparse entries and XenForo posts readable without media.
- Preserve mention suppression, URL sanitization, the 4,000-character Text Display budget, and the 40-component message limit.

## Non-goals

- Do not extract images from arbitrary HTML.
- Do not add interactive buttons or require an application-owned webhook.
- Do not change configuration keys, delivery IDs, polling, retries, or persistence.
- Do not replay previously delivered entries.

## Data model

Extend the frozen `EntryData` value object with backward-compatible defaults:

```python
discussion_url: str | None = None
image_url: str | None = None
categories: tuple[str, ...] = ()
```

Existing constructors remain valid. These fields are source-neutral: a future strategy may populate them without changing the renderer contract.

## RSS extraction

`RSSStrategy.get_entry_data()` will populate optional fields from structured feedparser values only.

### Discussion URL

- Read `entry.comments` when it is a non-empty string.
- Keep it only when it differs from the primary entry link.
- Invalid schemes or malformed URLs are omitted by the renderer's existing safe-URL boundary.

This exposes Hacker News article and discussion links separately. Reddit entries normally use the post discussion as their primary link, so they do not gain a duplicate link.

### Thumbnail

Select the first non-empty structured image candidate in this order:

1. `media_thumbnail[*].url`
2. `media_content[*].url` when its declared medium or MIME type identifies an image
3. `enclosures[*].href` or `enclosures[*].url` when its MIME type starts with `image/`

Do not inspect HTML content or follow URLs. The renderer validates the selected URL before placing it in a Thumbnail component.

### Categories

- Read terms from `entry.tags[*].term`.
- Trim whitespace, discard empty values, preserve order, and remove duplicates.
- Keep at most three terms, each truncated to 64 characters.

## XenForo extraction

Keep the current reliable mapping for thread title, post permalink, author, body, and timestamp. The optional `EntryData` fields retain their defaults. No post-HTML image extraction or speculative forumscraper field mapping is added.

## Source identity

The renderer derives a display label from fields it already receives:

- `feed.strategy == "xenforo"` → `Forum`
- RSS hostname ending in `reddit.com` → `Reddit`
- RSS hostname `news.ycombinator.com` → `Hacker News`
- Other RSS sources → `RSS`

The configured or fetched `source_title` remains visible alongside this label.

## Components v2 layout

Each message keeps one accent-colored Container.

### With a valid thumbnail

1. Section containing one or two Text Display children:
   - linked or plain level-two heading
   - description when non-empty
2. Thumbnail accessory using the validated image URL and a title-derived description capped at 1,024 characters
3. Separator with divider enabled and compact spacing
4. Metadata Text Display

### Without a valid thumbnail

1. Linked or plain heading Text Display
2. Optional description Text Display
3. Separator with divider enabled and compact spacing
4. Metadata Text Display

### Metadata

The first line contains the source label, source title, optional author, and optional relative timestamp. External text remains Markdown-escaped and bare-link-neutralized.

When a safe, distinct discussion URL exists, append a second subtext line with a static `Discussion` Markdown link. Append escaped category terms after that link, or on the second line by themselves when there is no discussion URL.

The primary title remains linked to `entry.link`. No buttons or action rows are introduced.

## Limits and fallback behavior

- Count all nested and direct Text Display content against the existing 4,000-character aggregate budget.
- Include richer metadata in the same heading/description/metadata allocation algorithm.
- Keep the existing behavior of dropping an oversized primary Markdown link before truncating visible text.
- Omit invalid optional image and discussion URLs rather than rendering plain attacker-controlled links.
- Keep the maximum layout below ten total components, including nested Section children and Thumbnail.
- Preserve `allowed_mentions: {"parse": []}`, `IS_COMPONENTS_V2`, `wait=true`, and `with_components=true`.

## Testing

Add failing-first tests for:

- RSS normalization of a distinct Hacker News discussion URL.
- Structured thumbnail candidate precedence and image MIME filtering.
- Ordered, deduplicated, bounded categories.
- Hacker News article title plus a separate Discussion link.
- Reddit source label with text-only fallback when no structured image exists.
- Section plus Thumbnail structure when a safe image exists.
- XenForo Forum label with the existing text-only post layout.
- Invalid optional URLs being omitted.
- Rich metadata and nested Text Displays remaining within the aggregate character and component limits.

Run the full pytest, Ruff, formatter, mypy, no-excuse, and diff checks. Manually render live-source-shaped Reddit, Hacker News, and XenForo fixtures and inspect their exact component trees. No production Discord post is required for verification.

## Rollout

No configuration or database migration is required. Only newly delivered entries use the richer layout. After merge, the existing image deployment and Watchtower flow can update the `oracle` host normally.
