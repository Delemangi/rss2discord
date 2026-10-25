# Gjirafa50 rolling observations

This opt-in command observes an explicitly listed set of known Gjirafa50 products.
It does **not** enumerate the current catalog, enroll price baselines, approve
changes, send Discord messages, or change scraper scheduling/configuration. There
is no delivery path at any change count.

## Scope and initial setup

Create a versioned JSON manifest using verified public product identities and
canonical detail URLs. Leave an unresolved URL as `null`; never guess a permalink
from a numeric ID. Use each feed's existing stable ID so the read-only source
cooldown gate can find its health records.

```json
{
  "version": 1,
  "items": [
    {
      "feed_id": "gjirafa50-com-products",
      "host": "gjirafa50.com",
      "product_id": "12345",
      "url": null
    }
  ]
}
```

The example intentionally has no fetchable URL. For mapped products, use only an
actually evidenced HTTPS detail URL on the matching storefront. Supported hosts
are `gjirafa50.com` and `gjirafa50.mk`; currencies are EUR and MKD respectively.
Credentials, queries, fragments, unsafe paths and cross-host URLs are rejected.
Discovery history supplies identity, **not historical price evidence**.

Initialize a separate observation state directory:

```sh
python -m rss2discord.gjirafa_observe --state-dir observation-state init --manifest manifest.json
python -m rss2discord.gjirafa_observe --state-dir observation-state status
```

Initialization and inspection are offline. The observation database has its own
fixed filename, schema marker and writer lock. Do not replace or modify the main
application database, pass it as observation state, delete writer-lock files, or
copy a foreign database over observation state. Reimporting a different manifest
is refused: adding, removing or replacing identities is an explicit scope change,
not an incidental consequence of running the tool.

## Bounded runs

Only `run` accesses the storefront. It requires a readable **live source-state
database** containing the manifest feeds' `health_state` rows. That database is
opened read-only, never initialized or migrated. A missing/unreadable gate or
unrecognized feed fails closed; an empty observation cooldown is not permission
to bypass a production cooldown. Do not use an old copy to evade current blocks.

```sh
python -m rss2discord.gjirafa_observe --state-dir observation-state run --source-state data/state.db --limit 25
python -m rss2discord.gjirafa_observe --state-dir observation-state report
```

A batch processes at most the requested number of manifest items (default 25,
maximum 100), not that many guaranteed successful requests. Unknown URLs and
blocked/failed observations receive explicit outcomes and cannot starve later
items. Each result and cursor advance commit together; interruption can require
a refetch, but must not skip an uncommitted item. Only one process owns the
observation job, and no write transaction spans network I/O.

The HTTP client has shared per-run limits: 200 physical requests, 64 MiB total
response bytes, 4 MiB per response, 20 seconds per transfer and 180 seconds total.
Redirects consume the same request/byte budget and must remain on the same
storefront. Requests are paced at least one second apart per host. There are no
automatic retries, browser impersonation, challenge solving, proxy routing or TLS
verification bypasses. Access denial/rate-limit cooldowns are persisted in the
observation state and honored after restart, alongside the read-only source gate.

## What an observation means

A scalar confirmation requires exact primary-product numeric identity, a matching
canonical detail URL, the expected currency, and agreement between the ID-specific
visible price and Product microdata. Generic price wrappers and recommendation
prices are not valid substitutes. Bundles/variants, zero placeholders, conflicting
metadata and ambiguous rounded prices fail closed. A price alone does not prove
availability; missing/denied/invalid data never means deletion or a zero price.

Observation timestamps describe when a response was checked, not a guarantee of
upstream freshness or an atomic catalog snapshot.

## Epochs and cumulative reports

A manifest pass completes after every listed identity has an outcome, including
unknown or blocked outcomes. This is **not catalog completeness** or proof that
every known product has a usable price.

The first epoch consists of initial observations, not an approved price baseline.
Each later epoch pins the preceding completed observation epoch as its comparison
reference. An identity is comparable only when both epochs contain valid scalar
observations. Unknown/blocked results remain incomparable rather than disappearing.

Reports count distinct changed IDs per feed cumulatively across the entire epoch,
including earlier batches. They do not reset comparison values after each slice.
More than 100 changed IDs in any one feed produces a review warning, not delivery authorization.
Even below that threshold this tool has **no** alerting or approval functionality.

Before any future alerting integration, separately review an exact eligible scope
and fresh initial baseline manifest. Preserve aggregate change accounting, unknown
coverage, holds, exact-target approval, batch ownership and claim fencing. Never
feed individual rolling slices to the existing complete-catalog delivery helper.
