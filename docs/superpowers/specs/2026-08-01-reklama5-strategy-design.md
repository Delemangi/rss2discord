# Reklama5 Search Strategy Design

## Goal

Add a generic Reklama5 search strategy that forwards newly visible listings from any supported Reklama5 search URL to Discord. The initial deployment will use the computer-parts category (`cat=584`), but no category, city, keyword, seller, condition, transaction, price, or category-specific facet is hard-coded.

The strategy monitors future listings after its first successful baseline. It does not import Reklama5's complete current inventory and does not report edits, renewals, reactivations, or price changes to an already delivered ad ID.

## Configuration Contract

The feed uses the existing `url` and a new `reklama5` strategy name:

```yaml
- id: "reklama5-computer-parts"
  name: "Reklama5 Computer Parts"
  url: "https://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0&includeOld=1&includeNew=1"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "reklama5"
```

The configured URL is the scope boundary. URL fragments are rejected. Query keys are compared case-insensitively for the three strategy-owned parameters: every occurrence of `SortByPrice`, `pageView`, and `page` is removed, then exactly one canonical occurrence is appended. All other query pairs, including repeated keys and blank values, retain their configured order and values.

- `SortByPrice=2`, forced to Reklama5's “newly entered first” mode.
- `pageView=1`, forced to the stable list layout.
- `page`, set to pages 1 through 3 for each fetch.

Accepted URLs use HTTPS, effective port 443, no credentials, host `reklama5.mk` or `www.reklama5.mk`, and path `/Search`, `/Search/`, `/Search/Index`, or `/Search/Index/`. The configured scheme, normalized host, and effective port become the exact allowed origin; redirects may canonicalize within the accepted search path family but may not switch between apex and `www`. A redirect target's effective query multimap must equal the requested page's multimap after case-insensitive normalization of the three strategy-owned keys; parameter reordering is allowed, but adding, dropping, or changing any filter is rejected.

## Architecture

### `reklama5.py`

Owns listing normalization and the `ScraperStrategy` implementation:

- `Reklama5Listing`: frozen normalized listing value.
- `Reklama5Strategy.fetch_entries()`: requests the fixed three-page recent window, parses and validates each search page, deduplicates numeric ad IDs, and returns `(listings, "Reklama5")` with listings in oldest-first delivery order.
- `get_entry_id()` and `get_entry_data()`: map normalized listings into the existing delivery model.

The strategy sets `seed_existing_on_first_fetch = True`. It leaves delivery-history and new-entry limits unset because a fixed three-page source window already bounds each fetch and a finite history limit would eventually fail a long-running feed.

### `reklama5_http.py`

Owns untrusted URL and HTTP behavior:

- Validates the configured Reklama5 search URL before requesting it.
- Builds page URLs while preserving arbitrary filters.
- Uses the project's established synchronous bounded-request conventions and a monotonic per-attempt scan budget.
- Disables ambient automatic redirects and follows only validated same-origin search redirects.
- Applies a 120-second absolute deadline to one complete fetch attempt; every request uses the smaller of 30 seconds and the remaining deadline.
- Applies a cumulative ten-redirect-hop limit, 2 MiB per-response limit, and 6 MiB per-attempt limit. Post-decompression bytes from redirect and final response bodies count against both applicable limits.
- Classifies connection failures, timeouts, HTTP 408, HTTP 429, and HTTP 5xx as retryable `FeedFetchError` values. Other HTTP statuses and URL, redirect, budget, parsing, or page-validation failures are permanent.
- Reuses `retries.parse_retry_after()` for delta-seconds and HTTP-date support and propagates the parsed value through the application-owned fetch retry policy.

The application remains responsible for retrying a complete three-page fetch. A retry starts again at page 1 with a fresh scan budget, matching existing marketplace behavior.

## Parsing and Data Mapping

Only organic `#sr-holder > .ad-top-div` rows participate in chronological discovery. Parsing uses the observed generic card selectors `.SearchAdTitle`, `.searchAdDesc`, `.search-ad-price`, `.city-span`, `.ad-category-div small`, `.ad-date-div-1`, and `.ad-image`.

- A `.promotedBtn` whose normalized text is exactly `Промовирано` marks its row as promoted and excludes that row. Any other normalized text on `.promotedBtn` invalidates the complete page so an upstream marker change fails closed at runtime.
- Keep `OglasResultsHighlighted` rows unless they also contain the explicit promotion marker.
- Ignore premium carousel links such as `a.ad-promoted-link`.
- Resolve `.SearchAdTitle[href]`, then require HTTPS, effective port 443, no credentials, the exact configured origin, path `/AdDetails`, and exactly one decimal `ad` query value. Generate the canonical detail URL from that validated ID instead of retaining the raw `href`.
- Require a non-empty title and detail link; skip malformed individual cards.
- Deduplicate IDs across all three pages; the first occurrence in recent-first scan order wins.
- Sort by ascending `activity_at`; equal timestamps reverse original scan position so newer-first source ties become oldest-first delivery ties.

Normalized fields:

- `entry_id`: numeric Reklama5 ad ID encoded as `EntryId`.
- `url`: absolute canonical detail URL.
- `title`: normalized card title.
- `summary`: normalized `.searchAdDesc` text, truncated to 2,000 characters after normalization.
- `price`: normalized visible price, including “По Договор” when present.
- `location`: normalized municipality/city text.
- `category`: normalized organic category label.
- `activity_at`: timezone-aware `datetime` localized to `Europe/Skopje`; only the eventual `EntryData.timestamp` is serialized to ISO format.
- `image_url`: absolute HTTPS thumbnail URL extracted from the card background image.

`EntryData` uses `title`, the canonical detail URL as `link`, the summary as `description`, an empty author, `activity_at.isoformat()` as `timestamp`, the image URL when present, and `(category,)` or `()` as `categories`. Source metrics are emitted in fixed `Price`, then `Location` order when their values are present.

## Timestamp Rules

The visible Reklama5 timestamp is mutable activity time, not immutable creation time. It is still needed because the current application globally rejects missing or timezone-naive timestamps when age filtering is enabled.

The parser accepts the observed public formats:

- `Денес HH:MM`
- `Вчера HH:MM`
- `DD/MM/YYYY HH:MM`
- `D <Macedonian month> HH:MM`

Month-only dates use the current `Europe/Skopje` year unless that would place the timestamp in the future, in which case the previous year is used. Ambiguous local times use `fold=0`; nonexistent local times are rejected. An injectable clock must return an aware datetime and makes relative-date tests deterministic. A card with an unparseable timestamp is skipped rather than assigned crawler time. `tzdata` is added as a runtime dependency so `ZoneInfo("Europe/Skopje")` also works on Windows and minimal containers.

## Page Validation and Empty Searches

A legitimate narrowly filtered search may contain zero organic listings. The strategy therefore keeps `require_entries_for_initialization = False` so an empty initial search initializes correctly and its first later listing is delivered.

Every valid page must contain form `#myFrom`, result container `#sr-holder`, and paginator `ul.pagination`. Live non-empty and zero-result responses both expose the current page through `#myFrom input[name=page]`; it must contain one positive decimal value equal to the requested page. Live non-empty paginated responses also expose `ul.pagination li.active`, which must agree with the requested page whenever paginator links exist. The explicit zero-result response has no active item because it has no paginator links.

Paginator links are evidence only and are never followed; requests are built independently from the configured URL. Each `ul.pagination a[href]` must resolve to HTTPS, effective port 443, no credentials, no fragment, the exact configured origin, and an accepted search path. Its query must preserve all configured filters plus the forced sort/layout values and contain exactly one positive decimal `page` value; only that page value may differ from the current request.

The explicit zero-result state is the result-count value in `span.float-left > span[style*="vertical-align"]` normalized to decimal zero, no valid organic rows, and no paginator page links. Promoted rows may still appear in `#sr-holder` and do not invalidate zero results. A terminal page is either that explicit zero-result state or a valid non-empty page whose paginator has no page number greater than the current page. Organic row count alone is never terminal evidence.

The fetch fails closed when it receives:

- Reklama5 application-error text.
- Challenge, login, homepage, or unrelated redirected HTML.
- Missing search-result structure without an explicit zero-result state.
- An inconsistent current-page marker or a paginator link outside the accepted scope.
- A non-terminal page before page 3 that yields no valid organic IDs.
- A pagination cycle, defined as a later requested page whose complete set of valid organic numeric IDs is non-empty and contains no ID not already seen on earlier requested pages.

Pages 2 and 3 are not requested after an explicit terminal page.

## Application Integration

- Add `reklama5` to `FeedStrategyName`.
- Export `Reklama5Strategy` from `transports`.
- Register it in `RSSToDiscord._strategies`.
- Add it to exhaustive non-price strategy handling. `price_check_interval` remains invalid for Reklama5.
- Add `SOURCE_LABEL_REKLAMA5 = "Reklama5"` and the exhaustive `reklama5` branch to `discord.source_labels.source_label()`.
- Add `tzdata` to runtime dependencies and refresh the lockfile.
- Document Reklama5 in the README and annotated example configuration.

No delivery-store schema, scheduler API, price snapshot, or adaptive frontier state changes are included.

## Testing Strategy

All production behavior is introduced test-first.

1. Parser fixtures and unit tests:
   - Rich computer-parts card mapping.
   - Generic category/filter cards.
   - Promoted and carousel exclusion.
   - Highlighted organic inclusion.
   - Malformed card skipping.
   - Price, location, category, image, and summary normalization.
   - Deterministic Macedonian relative and calendar timestamps.
   - Valid explicit zero-result page versus invalid empty HTML.
2. Pagination tests:
   - Preserve arbitrary configured query parameters.
   - Force sort, layout, and pages 1 through 3.
   - Stop only at selector-backed terminal evidence; row count alone does not stop pagination.
   - Deduplicate IDs with first occurrence winning and return deterministic oldest-first order, including equal timestamps.
   - Reject inconsistent current-page markers and duplicate-only pagination cycles.
   - Restart at page 1 after application-level retry.
3. HTTP tests:
   - URL/origin/path/fragment validation and case-insensitive replacement of forced query keys.
   - Exact-origin and filter-equivalent redirect handling plus cumulative redirect cap.
   - Exhaustive retryable/permanent status classification and shared `Retry-After` parsing.
   - Declared and streamed response limits, redirect-body accounting, and absolute scan deadline.
4. Integration tests:
   - Configuration accepts `reklama5` and rejects adapters/price monitoring.
   - Non-empty first successful fetch seeds existing IDs without delivery.
   - Empty first successful fetch initializes, and its first later listing reaches the Discord sender.
   - Changed title, price, or activity time for an already seeded or delivered numeric ID remains silent.
   - A Reklama5 payload renders the `Reklama5` source label without reaching `assert_never`.
5. Manual QA:
   - Run the strategy against the live category-584 URL.
   - Confirm three-page maximum, generic filter preservation, non-empty normalized listings, unique numeric IDs, and oldest-first output.

## Documentation and Operational Contract

Documentation states that:

- The strategy accepts generic Reklama5 search URLs.
- The category-584 example tracks computer parts and accessories.
- The first successful result window is a silent baseline.
- The strategy is a bounded best-effort future-listing feed, not a complete current-inventory importer.
- Same-ID edits, renewals, and price changes do not generate another notification.
- Three pages are fetched at most per ordinary feed cycle.
