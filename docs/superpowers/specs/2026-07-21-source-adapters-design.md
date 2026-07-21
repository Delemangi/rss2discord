# Source Adapters and Richer Feed Metadata

## Goal

Add optional source-specific adapters after feed transport parsing so Discord
cards can use metadata that generic RSS normalization cannot represent reliably.
Existing feeds without an adapter must keep their current behavior.

## Configuration

`strategy` remains responsible for fetching and parsing a transport. Supported
values remain `rss` and `xenforo`.

An optional `adapter` selects post-parse source behavior. This change supports:

- `hackernews` for RSS-discovered Hacker News items
- `reddit` for public Reddit RSS or Atom feeds

Adapters are only valid with the `rss` strategy. Omitting `adapter` preserves
the generic RSS path.

## Data flow

1. The configured strategy fetches entries and creates baseline `EntryData`.
2. Delivery history is checked before adapter work, so already-delivered entries
   never trigger enrichment requests.
3. The configured adapter receives the raw parsed entry and baseline data.
4. The adapter returns immutable enriched `EntryData`.
5. The existing Discord Components v2 renderer displays bounded source metrics
   in its existing metadata text component.

Adapters are additive and fall back to baseline RSS data when optional metadata
is absent or enrichment is unavailable.

## Structured metadata

`EntryData` gains an immutable tuple of label/value source metrics. Metric text
uses the renderer's existing metadata escaping and global 4,000-character text
budget. No additional Discord components are introduced.

Hacker News metrics are ordered as points, comments, and article domain. Reddit
uses categories for the subreddit and does not invent unavailable scores or
comment counts.

## Hacker News adapter

The RSS feed remains the discovery mechanism. The adapter extracts the numeric
item ID from a `news.ycombinator.com/item?id=...` discussion URL, then requests
the official v0 item endpoint.

Successful enrichment may provide:

- submitter username
- score
- total comment count
- self-post text
- canonical article URL
- article domain

Missing, dead, deleted, malformed, or unavailable API records leave the RSS
entry usable. Enrichment is capped at five unseen entries per feed poll to bound
sequential API latency; remaining entries use baseline RSS data. The adapter does
not traverse comment trees and does not fetch linked article pages.

## Reddit adapter

The credential-free adapter uses only public RSS or Atom content. It preserves
the Reddit permalink as the discussion URL and uses the feed's `[link]` target
as the primary link when one is supplied. It normalizes `/u/<name>` authors and
relies on existing feed categories and media-thumbnail parsing for subreddit and
thumbnail metadata.

Scores, comment counts, flair, NSFW state, galleries, and threading are not
reliably available in public feeds and are not inferred.

## Generic RSS improvements

Generic parsing gains two source-agnostic fallbacks:

- the first structured `content.value` when summary and description are absent
- raw `published` or `updated` strings when parsed time structs are absent

Existing HTML cleanup, truncation, URL safety, category bounds, and image MIME
checks continue to apply.

## Failure behavior

Transport failures keep the existing feed retry policy. Adapter enrichment is
optional: known HTTP, JSON, and validation failures are logged and return the
baseline entry instead of preventing delivery.

## Explicit exclusions

- arbitrary article-page or Open Graph scraping
- Reddit OAuth integration
- Hacker News comment-tree traversal
- feed-logo promotion to per-entry thumbnails
- database migrations or replaying delivered entries
- additional Discord component nodes for metrics
