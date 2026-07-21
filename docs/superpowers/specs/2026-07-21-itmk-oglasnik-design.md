# IT.mk Oglasnik Strategy Design

## Goal

Deliver useful Discord cards for new IT.mk Oglasnik listings without relying on the generic XenForo thread scraper. The strategy must avoid placeholder cards, make one HTTP request per poll, and preserve existing RSS and XenForo behavior.

## Scope

Add a new `itmk_oglasnik` feed strategy for server-rendered IT.mk Oglasnik index and category pages. The first version parses only data present on listing cards and does not fetch individual detail pages.

Extract:

- numeric listing ID and canonical listing URL
- title and summary
- price, listing type, and condition
- category and sold/locked status
- seller and creation timestamp
- expiration date and view count
- primary listing image when it is not the site's placeholder image

Do not extract contact information, seller-profile statistics, attachment galleries, warranty, cargo availability, or discussion URLs in this version.

## Architecture

Create `ITMkOglasnikStrategy` as a separate `ScraperStrategy` implementation. Keep generic `XenForoStrategy` unchanged because it operates on discussion threads and cannot parse the Oglasnik marketplace routes.

The new strategy will:

1. Fetch the configured index/category URL with redirects enabled, a bounded timeout, and the existing retry-compatible `FeedFetchError` contract.
2. Parse each `.structItem` card with a direct Beautiful Soup dependency into a frozen typed listing record at the external-data boundary.
3. Discard cards without a numeric listing ID, canonical listing link, meaningful title, or meaningful summary.
4. Reverse the site's newest-first cards so Discord receives them oldest-first, matching the RSS strategy's chronological delivery order.
5. Convert each record to `EntryData` after the application checks delivery history.

Add `itmk_oglasnik` to configuration and application strategy routing. Give it a distinct `IT.mk Oglasnik` source label.

## Discord Mapping

- `EntryData.title`: listing title
- `EntryData.link`: canonical `/oglasnik/<slug>.<id>/` URL
- `EntryData.description`: normalized card summary
- `EntryData.author`: seller name
- `EntryData.timestamp`: machine-readable creation time from the card
- `EntryData.image_url`: primary card image, excluding `no-product-image.png`
- `EntryData.source_metrics`: price, condition, listing type, expiration, and views when present
- `EntryData.categories`: product category followed by sold/locked status when present

The existing Components v2 renderer already handles metrics, tags, safe URLs, truncation, and thumbnails, so no renderer-specific Oglasnik branch is needed.

## Parsing Rules

- Resolve relative URLs against the final redirected response URL.
- Identify listings from canonical links ending in `.<numeric-id>/`.
- Read structured field values from the card's `dt`/`dd` pairs by visible Macedonian labels.
- Prefer semantic `time[datetime]` values over localized display text.
- Collapse repeated whitespace in title, summary, seller, category, status, and metric values.
- Ignore site-wide navigation/sidebar metadata by restricting parsing to each `.structItem` card.
- Do not invent values when a field is absent.

## Error Handling

- HTTP 429 and 5xx responses are retryable.
- Connection errors and timeouts are retryable.
- Other request errors and malformed/empty marketplace pages wait until the next scheduled poll.
- Error messages must not include configured URLs or query-string secrets.
- A page with no valid listing cards raises `FeedFetchError` rather than silently succeeding.
- Individual malformed cards are skipped so one bad listing cannot suppress valid siblings.

## Testing

Add deterministic HTML-fixture tests for:

- complete listing extraction and `EntryData` mapping
- relative URL resolution and numeric ID extraction
- missing optional metadata
- placeholder-image suppression
- sold/locked status extraction
- malformed and empty card rejection
- retryable and non-retryable request failures without URL leakage
- configuration acceptance and adapter restrictions
- source label and application strategy routing
- end-to-end Discord payload fields using the existing Components v2 builder

Run the full pytest, Ruff, and mypy gates. Manual QA will parse the live Oglasnik index through the strategy and build a real Discord payload locally without sending a webhook.

## Documentation

Update README and `config/config.example.yaml` with an IT.mk Oglasnik example using `https://forum.it.mk/oglasnik/`. Explain that this strategy targets Oglasnik index/category pages while `xenforo` remains for ordinary forum threads.

## Non-Goals

- Editing previously delivered Discord messages when listings become sold or locked
- Detail-page enrichment, galleries, warranty, cargo, or discussion links
- A generic marketplace framework for other XenForo installations
- Replacing the existing RSS or XenForo strategies
