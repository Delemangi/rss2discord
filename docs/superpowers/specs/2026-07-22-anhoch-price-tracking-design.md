# Anhoch Full-Catalog Price Tracking Design

## Summary

RSS2Discord will continue checking all configured feeds, including the latest Anhoch products, every five minutes. Anhoch feeds may additionally enable an hourly full-catalog scan that compares each product's current selling price with a persistent SQLite snapshot and sends Discord alerts for increases and decreases.

The first full scan and products without a previous snapshot establish silent baselines. Changes to the exact numeric selling amount or currency trigger alerts; formatting-only changes remain silent.

## Goals

- Preserve five-minute new-product alerts and their existing first-sync behavior.
- Check every Anhoch catalog product hourly when price tracking is enabled.
- Notify for both selling-price increases and decreases.
- Persist snapshots across restarts without a second service or database.
- Keep catalog traversal, response sizes, retries, and shutdown bounded.
- Avoid adding scheduling and catalog responsibilities to already-large modules.

## Non-goals

- Alerting on list-price changes when the selling price is unchanged.
- Alerting when a product first appears in the price snapshot.
- Tracking stock, installment, image, name, or description changes.
- Deleting historical prices when products disappear from the catalog.
- Running a second process, container, queue, or database.
- Providing per-product subscriptions or price thresholds.

## Configuration

Add an optional positive `price_check_interval` field to `FeedConfig`:

```yaml
refresh_interval: 300

feeds:
  - id: anhoch-new-products
    name: Anhoch New Products
    url: https://www.anhoch.com/products?inStockOnly=2
    webhook: https://discord.com/api/webhooks/ID/TOKEN
    strategy: anhoch
    price_check_interval: 3600
```

- Omitted or `null`: price tracking is disabled for that feed.
- Positive number: seconds between complete price scans.
- The field is valid only when `strategy` is `anhoch`.
- `refresh_interval` remains the cadence for ordinary feed and new-product processing.
- The active deployment will use `price_check_interval: 3600`.
- Startup validation errors must continue redacting webhook secrets; the new field is safe to name in validation locations.

Existing configurations remain valid and do not begin full-catalog scans implicitly.

## Architecture

### Runtime scheduler

Extract the current loop from `RSSToDiscord` into a focused, single-threaded runtime scheduler. It owns monotonic deadlines for two job types:

1. one ordinary all-feed cycle at `AppConfig.refresh_interval`;
2. one full-catalog price job per enabled Anhoch feed at that feed's `price_check_interval`.

Both job types run immediately at startup. When both are due, the ordinary feed cycle runs first so existing new-product behavior remains primary. After a job finishes, its next deadline is calculated from completion time. A missed deadline causes one immediate run, not multiple catch-up runs.

The scheduler sleeps until the nearest deadline through the existing interruptible sleep behavior. SIGINT and SIGTERM stop sleeps, fetch backoff, persistence retries, and post delays. Jobs remain sequential. If a price scan delays an ordinary deadline, the ordinary cycle runs next and is not discarded.

### Feed processing

`RSSToDiscord` continues to own ordinary entry processing, new-product first-sync seeding, age checks, Discord delivery, and delivery persistence. Its existing run loop moves to the scheduler.

Extract the existing HTTP fetch retry policy into a small reusable component so ordinary feed fetching and full-catalog price fetching share:

- three attempts;
- exponential backoff with jitter;
- bounded `Retry-After` support;
- interruptible shutdown;
- existing sanitized error reporting.

This extraction keeps `app.py` below the project's module-size ceiling instead of adding more responsibilities to its current 276 lines.

### Anhoch catalog client

Extract Anhoch HTTP models, page construction, response reading, validation, redirect handling, and pagination into a catalog client shared by:

- `AnhochStrategy`, which retains the latest-product window;
- `AnhochPriceMonitor`, which requests the full catalog.

The latest-product contract remains:

- `sort=latest`;
- 30 products per page;
- at most 3 pages;
- oldest-first delivery order after fetching.

The full-catalog contract is:

- `sort=latest`;
- 500 products per page;
- at most 100 pages, supporting 50,000 products;
- a 200 MiB aggregate ceiling derived from the 2 MiB per-response and 100-page limits;
- stop at the declared last page or first empty page;
- reject a catalog that still claims additional pages after page 100;
- retain original API order because comparison does not depend on delivery order.

Every response, including redirect bodies, is limited to 2 MiB. Existing status classification, timeout, redirect, schema validation, and retry metadata remain unchanged.

The validated selling-price model adds:

- exact non-negative decimal `amount` from the API string;
- non-empty localized `formatted` value;
- non-empty `currency` code.

Numeric amounts determine whether a change occurred and its direction. Formatted values are only presentation data.

### Price monitor

`AnhochPriceMonitor` owns one complete scan for one feed:

1. fetch and validate the entire catalog in memory;
2. load all stored snapshots for the feed;
3. classify each current product as unseen, unchanged, formatting-only changed, increased, decreased, or currency-changed;
4. batch-persist unseen and formatting-only snapshots without notifications;
5. send changed products sequentially;
6. persist each new snapshot only after Discord accepts its alert.

No SQLite mutation occurs until all catalog pages have been fetched and validated. One failed page therefore cannot create a partial catalog snapshot.

## Persistence

Add an automatically created SQLite table:

```sql
CREATE TABLE IF NOT EXISTS anhoch_price_snapshots (
    feed_id TEXT NOT NULL,
    product_id INTEGER NOT NULL,
    amount TEXT NOT NULL,
    formatted TEXT NOT NULL,
    currency TEXT NOT NULL,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch()),
    PRIMARY KEY (feed_id, product_id)
) WITHOUT ROWID;
```

`amount` is stored as canonical decimal text to avoid binary floating-point changes. The store exposes batch load and transactional upsert operations using a typed snapshot value rather than passing individual primitive fields.

Persistence rules:

- No existing row: insert silently.
- Same amount and currency, same formatting: no write or alert.
- Same amount and currency, changed formatting: update silently.
- Different amount in the same currency: send an increase or decrease alert, then update after successful delivery.
- Different currency: send a neutral `Price changed` alert because values in different currencies cannot be ordered reliably, then update after successful delivery.
- Product absent from a scan: retain its row unchanged.
- Product returns later: compare with its last stored snapshot.

Baseline inserts may commit before changed-product delivery because they do not affect alert retry semantics. Changed snapshots commit individually after successful sends.

If Discord rejects an alert, retain the previous snapshot so the same change is retried on the next hourly scan. SQLite busy or locked errors use the existing interruptible retry policy. A permanent database error stops the current job and reaches the scheduler's safe error boundary.

As with ordinary entries, a crash after Discord accepts a message but before SQLite commits can cause one duplicate alert after restart. No distributed transaction is introduced.

## Discord alert behavior

Price alerts reuse the configured feed webhook identity, source label, accent color, product title, product link, image, stock, installments, and current list-price context.

Descriptions:

- decrease: `Price decreased from <old> to <new>`
- increase: `Price increased from <old> to <new>`
- currency change: `Price changed from <old> to <new>`

Metrics begin with:

- `Price: <new formatted selling price>`
- `Previous: <old formatted selling price>`

The existing `Original`, `Stock`, and `Installments` metrics follow when applicable. `Original` continues to mean the current list price and is not overloaded with historical data.

The monitor applies the existing `delay_between_posts` between accepted price alerts. A failed alert does not prevent later changed products in the same scan from being attempted.

## Failure handling and bounds

- Full scans retry as a complete operation. A retry starts again at page one.
- Non-retryable HTTP, redirect, response-size, and schema failures abort the scan.
- Retryable HTTP and network failures use the shared three-attempt policy.
- No snapshots change when fetching or validation fails.
- The 2 MiB limit applies independently to every response.
- More than 100 non-empty catalog pages is an explicit failure, not a partial success.
- Unexpected duplicate product IDs with conflicting prices make the scan invalid; identical duplicates are collapsed.
- Scheduler error boundaries log the feed ID and typed failure without logging its URL or webhook.
- Shutdown during any wait ends the current operation without beginning another job.

## Tests

### Catalog transport

- Latest-product pagination remains 30 products and 3 pages.
- Full catalog requests 500 products per page and reaches the declared final page.
- Empty pages stop traversal.
- A catalog beyond 100 pages is rejected.
- Declared and streamed responses over 2 MiB are rejected.
- A later-page failure returns no partial catalog.
- Exact amount, formatted value, and currency are validated.
- Conflicting duplicate IDs reject the scan.

### SQLite store

- Snapshots are isolated by feed ID and product ID.
- Decimal text, formatting, and currency persist after reopening.
- Batch upserts are atomic.
- Existing delivery tables continue to work after automatic table creation.

### Price monitor

- First full scan seeds silently.
- A later unseen product seeds silently.
- Equal amounts send nothing.
- Formatting-only changes update without an alert.
- Decreases and increases render the correct old and new values.
- Currency changes use neutral wording.
- Failed Discord sends retain the old snapshot and retry next scan.
- Successful sends persist and do not repeat after restart.
- Missing products retain their history.
- One failed product delivery does not suppress later changes.

### Scheduler and configuration

- Ordinary and enabled price jobs both run immediately at startup.
- Ordinary cycles repeat every 300 seconds while price scans repeat every 3,600 seconds.
- An overdue job runs once immediately and then receives a new deadline.
- Interrupting scheduler sleep exits cleanly.
- `price_check_interval` accepts a positive value only for Anhoch feeds.
- Existing configuration without the field remains valid and disables price tracking.
- Validation output does not expose webhook secrets.

### Regression and quality gates

- Existing new-product first-sync tests remain green.
- Existing fetch retry, persistence retry, source-label, and Discord component tests remain green.
- Run the complete pytest, Ruff, mypy, and package build gates.
- Run diagnostics on every changed Python file.

## Manual QA

Drive the installed `rss2discord` CLI against a local HTTP catalog fixture and a local webhook-capture server:

1. Start with one catalog product and confirm the immediate ordinary cycle and price scan produce no backfill messages while storing baselines.
2. Repeat the same catalog and confirm no message.
3. Lower the numeric selling amount and confirm exactly one decrease alert with old/new values and normal Anhoch metadata.
4. Repeat unchanged and confirm no duplicate.
5. Raise the amount and confirm exactly one increase alert.
6. Add a new product and confirm the ordinary path announces it while the price monitor only seeds its price.
7. Return an oversized or malformed page and confirm no snapshot changes.
8. Stop the process during scheduler sleep and confirm a clean exit.

## Deployment

- SQLite creates the new table automatically; no destructive migration is required.
- Add `price_check_interval: 3600` only to the active Anhoch feed.
- Keep `refresh_interval: 300` unchanged.
- Build and publish the normal image, restart the existing Compose service, and inspect startup logs for both scheduled cadences.
- The first production full scan is intentionally silent and establishes roughly 10,000 baselines.

## Accepted trade-offs

- Jobs are sequential; a large price-alert burst may delay, but not discard, an ordinary feed cycle.
- Historical rows are retained indefinitely; at the current catalog size their SQLite cost is small.
- A narrow crash window can duplicate an accepted alert, matching existing delivery semantics.
- Full-scan retries repeat already successful page requests to guarantee all-or-nothing comparison input.
