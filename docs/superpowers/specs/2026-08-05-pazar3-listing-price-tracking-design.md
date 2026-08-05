# Pazar3 Listing and Price Tracking Design

## Goal

Add Pazar3 as a first-class feed strategy for bounded new-listing discovery and optional price-change monitoring over a configured public category or search scope. Preserve the delivery, retry, and fail-closed guarantees used by Reklama5 without claiming complete coverage for Pazar3 scopes that cannot be traversed respectfully.

## Source Contract

Pazar3 exposes unauthenticated server-rendered listing pages under `https://www.pazar3.mk/oglasi/...`.

- The default public route is newest-first and pagination uses `Page=N`.
- A result page contains 50 organic rows plus up to three separately placed rows inside `.top-positioned`.
- Organic rows expose a decimal `data-product-id`, a detail link ending in the same decimal ID, title, displayed publication or renewal time, category and location links, current price, currency, and an optional first-party thumbnail.
- Detail pages and localized URLs preserve the same numeric product ID. Product JSON-LD repeats that ID as `sku` and `productID` and exposes structured prices, but ordinary discovery and scoped catalog monitoring do not require one detail request per listing.
- Prices may be positive MKD or EUR values. Missing, negotiable, malformed, zero, and negative values are unavailable.
- The visible timestamp may represent publication or renewal. A known numeric ID never becomes a new listing solely because its timestamp or mutable fields changed.
- Pazar3's `robots.txt` declares `Crawl-delay: 20`. All Pazar3 requests in one process therefore share a serialized host-wide pacer with at least 20 seconds between request starts.

The undocumented `/mk/Home2/Search` and `/mk/Home2/GetGallery` browser endpoints are not part of the provider contract. The implementation uses only public server-rendered HTML routes.

## URL and Transport Boundary

Accept only credential-free, fragment-free HTTPS URLs on exact host `pazar3.mk` or `www.pazar3.mk`, effective port 443, and Macedonian listing paths below `/oglasi/`. The configured path and caller-owned filters define the feed scope. The provider owns and replaces the case-insensitive `Page` query key.

Redirects must preserve scheme, exact normalized host, port, configured path, and normalized query. Cross-origin, path-changing, filter-changing, credential-bearing, fragment-bearing, malformed, and excessive redirects fail closed.

Use an isolated curl-cffi transfer with certificate verification, browser TLS impersonation, no ambient proxy or netrc credentials, no inherited cookies, explicit headers, and streamed response limits. Each response is capped at 2 MiB. Discovery receives a 6 MiB and 120-second attempt budget. Complete catalog attempts receive a 20 MiB and 300-second budget. Retryable failures use the existing three-attempt policy, while the host-wide pacer remains effective across retries and jobs.

## Page Validation and Parsing

Every page must contain exactly one result-range marker, result list, paging control, and active page marker. Validate the displayed range, total result count, active page, and every paginator target against the configured scope.

Parse rows as follows:

1. Separate `.top-positioned` rows from organic rows before counting or retaining products.
2. Validate that each organic row has one decimal `data-product-id` and a same-origin detail URL whose final path segment matches that ID.
3. Deduplicate by numeric ID. A product observed organically remains eligible even if the same ID also appears in the promoted block.
4. Parse the title, current display timestamp, category, location, price, currency, and optional HTTPS image on exact host `media.pazar3.mk`.
5. Skip malformed listing content while retaining its valid numeric ID for first-fetch initialization, preventing a later parser repair from generating a false new-listing alert.

Reject unknown structural changes, contradictory IDs, invalid paginator links, duplicate page contents, empty non-terminal pages, and impossible result ranges. A result count of zero is valid only when no organic rows or forward pages exist.

## New-Listing Discovery

`Pazar3Strategy` uses `seed_existing_on_first_fetch = True` and scans at most the first three newest pages.

- The first successful scan silently records every observed organic ID, including IDs whose mutable card fields could not be parsed.
- Later scans deliver only previously unseen organic IDs.
- Listings are delivered oldest-first by parsed displayed timestamp, with stable scan position as a tie-breaker.
- Renewals and edits of known IDs remain silent.
- Promoted-only rows never enter the discovery ledger.
- Discovery is explicitly a recent three-page observation window and does not claim complete historical inventory coverage.

## Scoped Price Monitoring

Allow `price_check_interval` for `strategy: pazar3`. Price monitoring traverses the complete configured public scope independently from three-page discovery, subject to these limits:

- at most 10 pages;
- at most 500 organic listings;
- 50 expected organic rows per non-terminal page;
- stable total result count throughout the scan;
- no duplicate-page cycle or conflicting duplicate ID;
- complete final organic row count equal to the advertised result count.

A scope advertising more than 500 results or requiring more than 10 pages fails immediately with a bound error. The full electronics category is intentionally unsupported.

For each valid positive price, compare `(amount, currency)` where currency is `MKD` or `EUR` without conversion.

- The first valid observation is a silent baseline.
- Equal amount and currency produce no alert.
- Formatting-only changes update the snapshot silently.
- Amount changes produce increased or decreased alerts.
- Currency changes produce a neutral currency-change alert rather than a numeric increase or decrease.
- Missing or invalid prices do not create snapshots or replace previous valid values.
- Products removed from the scope do not emit alerts or delete snapshots.
- A changed snapshot is persisted only after Discord accepts its alert.

Retain at most 10,000 snapshots per feed and deliver at most 100 price changes per scan. Exceeding either bound sends no affected alerts and advances no affected snapshots.

## Runtime Integration

- Extend `FeedStrategyName` with `pazar3` and permit its optional positive price interval.
- Register and export `Pazar3Strategy`.
- Create one shared Pazar3 request pacer in `RSSToDiscord`; inject it into discovery and the price-monitor runtime so all Pazar3 feeds and retries obey the host-wide delay.
- Build `Pazar3CatalogClient` and `Pazar3PriceMonitor` through the existing provider-specific monitor factory path.
- Reuse feed-scoped SQLite delivery and price snapshot persistence.
- Add the `Pazar3` source label, example configuration, and explicit bounded-coverage documentation.

## Failure and Interruption Behavior

Every page request checks shutdown before pacing, after pacing, and before publishing parsed results. Interruption prevents partial discovery initialization and partial catalog return.

Catalog retries restart at page one. Scope, redirect, response-size, page-schema, promotion separation, count-drift, cycle, page-limit, product-limit, and deadline failures are explicit `FeedFetchError` causes. Failed scans neither initialize a discovery feed nor mutate price snapshots.

The pacer uses the runtime's interruptible sleep callback. Shutdown during the 20-second wait stops without issuing the request. The first request may start immediately; subsequent Pazar3 requests in the same process wait until the shared host interval has elapsed.

## Testing and Manual Verification

Follow red-green-refactor with focused tests for:

1. URL scope, pagination ownership, redirects, isolated TLS session behavior, response budgets, retries, and interruptible host-wide pacing;
2. page structure, result ranges, promoted placement, organic counts, identity agreement, localized timestamps, images, malformed rows, duplicates, and terminal-page detection;
3. three-page discovery traversal, first-fetch initialization, renewal silence, ordering, cycles, bounds, and shutdown;
4. complete scoped catalog traversal, count drift, page and product limits, retries, and incomplete scans;
5. MKD and EUR price baselines, amount and currency changes, unavailable prices, delivery retries, snapshot and change limits, and persistence ordering;
6. configuration, runtime factory wiring, source labels, example configuration, and README behavior;
7. a bounded live smoke request through the production Pazar3 transport and parser without sending Discord messages.

The full existing pytest, Ruff, and mypy suites must remain green.
