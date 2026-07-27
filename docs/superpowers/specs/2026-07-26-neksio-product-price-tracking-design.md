# Neksio Product and Price Tracking Design

## Goal

Add Neksio as a first-class feed strategy that catches newly listed products and optionally reports price changes with the same delivery guarantees used by the existing Anhoch and Setec integrations.

## Source Contract

Neksio exposes an unauthenticated first-party catalog at `https://g.store.neksio.mk`.

- `GET /` contains the current top-level categories in `div.side-menu-category[data-bs-target="#subcat_{id}"]` elements.
- `POST /FilterAndPaginateProducts` returns validated JSON product cards for one category.
- Product IDs are stable integers and detail pages use `/Product/Details/{productId}`.
- Product cards expose the current tax-inclusive price, optional old price, product code, category, manufacturer, image path, and stock quantity.
- The site has no trustworthy created timestamp or newest-first ordering. Its `#new` view is curated and limited, so it cannot be the authoritative discovery source.

## Architecture

### Catalog adapter

Create a Neksio catalog adapter that:

1. Fetches the Neksio homepage and parses unique category IDs.
2. Requests every category through `POST /FilterAndPaginateProducts` with a fixed page size.
3. Continues through the reported page count within explicit category, page, response-size, redirect, and total-product bounds.
4. Validates responses with frozen Pydantic models.
5. Deduplicates products by numeric product ID and rejects conflicting duplicate records.

All redirects must remain on the configured origin. Homepage and JSON bodies must be streamed and capped before parsing. Network and response failures must become `FeedFetchError` values with the same retry classification used by existing transports.

### New-product strategy

Add `NeksioStrategy` with `seed_existing_on_first_fetch = True`. Every successful scan returns the bounded full catalog. The ordinary delivery ledger silently seeds all current product IDs on the first scan and reports unseen IDs on later scans.

Because Neksio does not expose a creation timestamp, each parsed catalog scan records its observation time. New-product entries use that timestamp so normal age filtering does not discard products that were first observed after the silent baseline.

Discord entry data includes:

- product name and numeric detail URL;
- current price and optional old price;
- product code, manufacturer, and stock metrics when present;
- category and subcategory labels;
- an absolute first-party image URL when available.

### Price monitoring

Allow `price_check_interval` for `strategy: neksio`. Add a Neksio price monitor that uses the same catalog adapter and persistent `PriceSnapshot` store as Anhoch.

- The first successful full scan stores a silent baseline.
- Unchanged numeric price and currency produce no notification.
- Formatting-only changes update the snapshot silently.
- Numeric or currency changes create one sequential Discord alert per product.
- A changed snapshot is persisted only after Discord accepts the alert, preserving retry behavior after failed delivery.
- Products first seen by the price monitor are baselined silently; product removal does not emit an alert.

Neksio prices use MKD and the API's tax-inclusive current amount. The optional old price is display metadata, not the comparison value.

## Runtime Integration

- Extend the strategy literal with `neksio`.
- Register and export `NeksioStrategy`.
- Permit `price_check_interval` for Anhoch, Neksio, and Setec only.
- Build the appropriate provider-specific price monitor from the existing scheduler path.
- Reuse the source-neutral feed-scoped snapshot persistence because feed IDs are globally unique and Neksio product IDs have stable string keys.
- Add a Neksio source label. Neksio images remain ordinary remote thumbnails unless testing demonstrates that Discord requires the Anhoch attachment workaround.

## Bounds and Failure Behavior

The implementation will define conservative constants for:

- homepage and catalog response bytes;
- streamed chunk size;
- same-origin redirects;
- top-level category count;
- pages per category;
- products per page and total unique products.
- scan-wide response count, bytes, and elapsed time.

Cross-origin redirects, malformed category markup, malformed JSON, inconsistent pagination, duplicate IDs with conflicting data, and exceeded bounds fail the scan. A failed scan neither initializes the discovery feed nor mutates price snapshots.

## Testing

Follow red-green-refactor with focused tests for:

1. category parsing, pagination, deduplication, response bounds, redirects, and invalid payloads;
2. product-to-entry mapping and first-fetch seeding through the app strategy path;
3. silent price baseline, price increases/decreases, formatting-only updates, failed delivery retries, and snapshot persistence;
4. configuration validation, runtime job wiring, source labels, README, and example configuration;
5. a live manual smoke request against the public Neksio surface using bounded catalog logic without sending Discord messages.

The complete existing test, lint, and type-check suites must remain green.
