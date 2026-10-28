# Price-monitoring flow

Price scans share a delivery policy, not a universal provider implementation.
Providers retain their catalog validation, availability rules, snapshot construction,
and any product-detail confirmation. The shared entry points remain importable from
`rss2discord.transports.price_monitor`.

## Where code belongs

The `transports/price_monitor/` package separates policy from side effects:

| Module | Responsibility |
| --- | --- |
| `comparison.py` | Pure snapshot classification and price direction |
| `contracts.py` | Store protocols, dependency contracts, and delivery plans |
| `planning.py` | Recovery state, safety gates, bounded selection, and health |
| `delivery.py` | Sequential attempts, claims, acknowledgments, and persistence |
| `catalog.py` | Scan preparation and complete-catalog orchestration |
| `__init__.py` | Stable public entry points used by provider monitors |

Internal modules import their collaborators directly rather than through the
package entry points. This keeps dependencies one-way: catalog orchestration uses
comparison, planning, and delivery; delivery uses planning; the stages share the
contracts. Provider modules adapt source-specific products to this flow.

## Stages

1. **Fetch and validate.** The provider establishes the catalog evidence and enforces
   its request, byte, time, and retained-snapshot limits. A partial catalog must not
   become a complete baseline merely because fetching returned some products.
2. **Compare snapshots.** `diff_price_snapshots` is a pure comparison in observation
   order. New products and formatting-only changes are silent updates; amount or
   currency changes produce change records. Missing products do not erase history.
   Comparison does not authorize delivery, release holds, or write to the database.
3. **Plan.** `prepare_price_delivery` excludes held identities, checks currency and
   recovery state, quarantines oversized change sets, and selects bounded delivery
   work. A plan selects work; it does not claim an attempt or send a notification.
4. **Confirm where required.** Providers such as CCCenter and Setec perform their
   own confirmation before delivery. Complete-catalog providers use their validated
   observations without adopting another provider's confirmation policy.
5. **Deliver and persist.** Apply permitted silent updates, process selected
   changes in order, and persist confirmed sends. Approved recovery deliveries
   require a claim; unsuccessful sends release it. Successful sends are followed
   by persistence and spacing before the next send. Attempt limits are not a
   guarantee that every selected message reaches Discord.
6. **Record health.** Record the completed scan's state without treating a blocked
   or quarantined plan as a healthy delivery cycle.

DDStore and Hivetec additionally support availability holds: a returning product
can acquire a new baseline silently only after the existing reconciliation checks
permit it. Review holds remain separate and are not automatically released.

## Boundaries to preserve

- Keep shutdown checks around potentially interruptible work, including message
  preparation and inter-message waits.
- Preserve claim generations and late-acknowledgment rules. Refactoring must not
  authorize a fresh send after revocation or make an already-sent message unsent.
- SQLite transactions remain owned by the persistence layer; moving orchestration
  code must not move writes outside those transactions.
- Discord transport retry policy is separate from price selection and recovery.
  Rate-limit responses, server errors, image fallback, and interrupted waits retain
  their distinct behavior.
- Delivery remains at-least-once. An ambiguous timeout or crash may cause a
  duplicate; this structure does not introduce exactly-once delivery.

## Regression checks

| Contract | Tests |
| --- | --- |
| Pure snapshot classification | `tests/test_price_snapshot_diff.py` |
| Public entry points, conversion order, claim release, and delivery spacing | `tests/test_price_monitor_flow.py` |
| Normal-delivery rotation | `tests/test_normal_price_fairness.py` |
| Recovery claims and concurrent state changes | `tests/test_recovery_concurrency.py`, `tests/test_price_recovery_integration.py` |
| Returning unavailable products | `tests/test_availability_hold_runtime.py`, `tests/test_availability_hold_store.py` |
| Provider-specific confirmation and comparison | `tests/test_cccenter_price_monitor.py`, `tests/test_neksio_price_monitor.py`, `tests/test_setec_price_confirmation.py` |
| Discord retry and fallback ordering | `tests/test_discord_client_retry_edges.py`, `tests/test_discord_image_retries.py`, `tests/test_discord_image_fallback.py` |

Run the full pytest suite, Ruff lint and formatting checks, mypy, and a package
build after reconciling changes across these boundaries. Provider-specific tests
are useful during implementation but do not replace the final shared-flow checks.
