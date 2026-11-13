# Price-monitoring flow

Price scans share a notification pipeline, not a universal provider implementation.
Providers own source-specific fetching, validation, and any product-detail
confirmation. The shared stages are importable from
`rss2discord.transports.price_monitor`.

## Where code belongs

| Module | Responsibility |
| --- | --- |
| `comparison.py` | Pure snapshot classification and price direction |
| `contracts.py` | Store protocols, dependency contracts, and delivery plans |
| `planning.py` | Currency validation, inline/digest selection, and health |
| `delivery.py` | Baseline/outbox persistence and inline card delivery |
| `digest.py` | Digest message, report attachments, and outbox draining |
| `scan.py` | Shared bounded scan, confirmation, and delivery orchestration |
| `__init__.py` | Stable public entry points used by provider monitors |

Providers fetch and validate their catalogs, then supply observations and card
rendering to `run_price_scan`. They do not implement their own approval,
quarantine, batch lifecycle, snapshot limits, diffing, or delivery loop.

## Stages

1. **Fetch and validate.** The provider establishes the catalog evidence and
   enforces its request, byte, time, and retained-snapshot limits. A partial
   catalog must not become a complete baseline merely because fetching returned
   some products.
2. **Compare snapshots.** `diff_price_snapshots` classifies observations. New
   products and formatting-only changes are silent baseline updates; amount or
   currency changes produce change records. Missing products do not erase history.
3. **Plan.** Currency mismatches raise `FeedFetchError("CurrencyChanged")`.
   Up to 25 changed products use individual cards; a larger scan uses one digest.
   There is no approval or quarantine state.
4. **Confirm where required.** Setec verifies variant identity, currency, and
   amount against product details. CCCenter verifies product details in bounded
   batches. Confirmed changes are reported; unconfirmed changes are excluded from
   the baseline and retried on a later scan.
5. **Persist.** `record_price_scan` writes the new baselines and the pending
   notification rows in one transaction. No network work happens inside that
   transaction.
6. **Deliver.** Pending groups drain oldest-first. A group of at most 25 rows
   sends one card per product. A larger group sends one or two compact briefs:
   header direction totals and category bars, then up to 20 largest drops and 20
   largest rises with links and percentages. The complete HTML and CSV reports
   attach once, to the final brief. The HTML report is self-contained and
   searchable/sortable. Rows are marked sent only after Discord confirms every
   brief in the group.
7. **Record health.** A drained outbox records `healthy` or `empty`. A failed
   send records `failed` with cause `DeliveryRetryPending` and leaves the rows
   pending.

## Outbox semantics

- A baseline advance and its notification commit atomically, so a crash cannot
  lose a change without also leaving the old baseline in place.
- Failed or interrupted sends retain pending rows. The next scan drains them
  before sending new work.
- Delivery is at-least-once. A crash after Discord accepts a message but before
  the local `sent` update can produce a duplicate.
- Reports contain every row: title, link, category, previous price, current
  price, currency, and observation time. Files are capped at 50,000 rows and
  8 MiB; overflow is marked explicitly in the final line rather than truncated
  silently.
- The HTML report is self-contained: filter by text or direction and sort any
  column without a network request. CSV fields that begin with `=`, `+`, `-`, or
  `@` are prefixed to prevent spreadsheet formula execution.
- The digest lists only moves inside a plausible 0.1×–10× band; more extreme
  values are counted and left in the report, so a bad source value cannot
  dominate the headline.

## Upgrade migration

Opening an older database performs an idempotent migration:

- The retired price-batch, hold, reconciliation, and baseline-approval tables are
  dropped, child tables first.
- `price_normal_delivery_cursors` is copied to `price_confirmation_cursors` and
  dropped.
- Historical `unknown`, `quarantined`, and `recovery_required` health states
  become `failed` with cause `LegacyState`.
- `price_snapshots`, `delivered_entries`, `initialized_feeds`, `health_state`,
  and existing `price_notifications` rows are preserved.

Take a consistent database backup before the first start with this version; the
dropped tables are not recreated.

## Discovery delivery

Ordinary new items follow the same presentation rule: up to 25 send individual
cards, and larger batches send one digest with up to three cards plus complete
HTML and CSV attachments. Entries are marked delivered only after Discord
confirms, and there are no new-entry or delivery-history caps that can stop a
feed permanently.

## Boundaries to preserve

- Keep shutdown checks around interruptible work, including message preparation
  and inter-message waits.
- SQLite transactions remain owned by the persistence layer; no database
  transaction spans a Discord send.
- Provider request, byte, and time budgets, redirect rules, and pacing remain
  provider-owned and unchanged.
- Discord transport retry policy is separate from price notification selection.
  Rate-limit responses, server errors, image fallback, and interrupted waits
  retain their distinct behavior.
- Invalid or incomplete source evidence must never overwrite a valid price.

## Regression checks

| Contract | Tests |
| --- | --- |
| Pure snapshot classification | `tests/test_price_snapshot_diff.py` |
| Inline boundary, digest, reports, retries, atomic persistence | `tests/test_price_notification_outbox.py` |
| Shared scan limits, confirmation rotation, silent updates, health | `tests/test_price_scan_pipeline.py` |
| Public entry points, conversion order, and delivery spacing | `tests/test_price_monitor_flow.py` |
| Provider-specific confirmation and comparison | `tests/test_cccenter_price_monitor.py`, `tests/test_neksio_price_monitor.py`, `tests/test_setec_price_confirmation.py` |
| Discord retry and fallback ordering | `tests/test_discord_client_retry_edges.py`, `tests/test_discord_image_retries.py`, `tests/test_discord_image_fallback.py` |

Run the full pytest suite, Ruff lint and formatting checks, mypy, and a package
build after reconciling changes across these boundaries. Provider-specific tests
are useful during implementation but do not replace the final shared-flow checks.
