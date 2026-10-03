# Scraper recovery operations

This describes the phase-2 recovery implementation, not a deployment instruction or a claim that every upstream scraper works. Deployment remains a separate operator decision. All admin commands are local and make no source requests or Discord sends; approval permits a later running monitor to send.

## State upgrade and backup

The application and admin CLI open the same SQLite database. Opening it creates missing recovery/baseline/health/cursor tables and additively adds missing health timestamp and delivery-claim columns. Existing delivery rows and price snapshots are preserved; startup does not approve price batches or invent historical health timestamps. The older Anhoch-specific snapshot migration still copies its rows into the shared snapshot table and removes the legacy table.

Before opening an existing database with the new version:

1. Identify the actual persisted database and save its configuration and application revision. Prefer an explicit `--database` path: a wrong/missing path creates a new database, and even `list`/`show` opens the store and may upgrade its schema.
2. Make a consistent backup. For offline copying, stop **all** writers and database clients first, then copy the database and any remaining `-wal`/`-shm` sidecars as one quiescent set. Do not delete a WAL to make copying easier: it can contain committed state. Do not copy only a live `state.db` or independently copy changing sidecars.
3. Alternatively, use a SQLite-aware [online backup](https://www.sqlite.org/backup.html) to a new destination. Verify the backup with an integrity check and a restore rehearsal on an isolated copy. Keep it separate from the working database.
4. Rehearse schema opening and inspection on that copy without starting the delivery service. No reset of delivery history or price snapshots is required for recovery.

The examples below assume the package is installed in the active environment. `python -m rss2discord.admin` is equivalent to the `rss2discord-admin` entrypoint. Replace `data/state.db`, feed IDs, batch ID `42`, and `FULL_FINGERPRINT` with inspected values; fingerprints must be copied in full, never abbreviated. `--database` goes **before** the subcommand. Without it, the CLI uses `STATE_DB_PATH`, then `data/state.db`.

```sh
python -m rss2discord.admin --help
python -m rss2discord.admin --database data/state.db health list
```

`--help` exits before opening a database. JSON output is intended for inspection. Reasons are stored audit text: use a meaningful nonempty reason without credentials.

## Price quarantine and approval

These are separate limits:

| Limit | Behavior |
| --- | --- |
| More than **100** changed prices | Persist a candidate and quarantine before sends or affected snapshot advancement. Exactly 100 is eligible for normal bounded processing. |
| **10** delivery attempts per price scan | Applies to normal and approved processing; failures consume the allowance, so this is not ten guaranteed deliveries. |
| **150,000** manifest items | Hard storage safety bound, not the approval threshold. Provider catalog limits can be lower. |

Price recovery is wired into DDStore/Hivetec and the shared adapter path used by Anhoch, CCCenter, Gjirafa50, Neksio, Neptun, Pazar3, Reklama5, Setec, and Technomarket. Provider completeness, price validity, capacity, and access checks still apply; a configured monitor is not evidence of working live access.

Inspect before approving:

```sh
python -m rss2discord.admin --database data/state.db price list --feed-id ddstore
python -m rss2discord.admin --database data/state.db price show 42 --offset 0 --sample-limit 100
python -m rss2discord.admin --database data/state.db price show 42 --offset 100 --sample-limit 100
python -m rss2discord.admin --database data/state.db price approve --feed-id ddstore --fingerprint FULL_FINGERPRINT --reason "Reviewed catalog repricing and exact targets"
```

`list` reports batch IDs, status, full fingerprint, catalog/available/item counts, pending/delivered counts, and audit timestamps/reason. `show` returns a page in `items_sample`, preserving every selected product ID, old/target amount, formatted price, currency, status, and attempt count. `--offset` is zero-based and nonnegative; `--sample-limit` accepts 1–1000 (default five). Each page reports `offset`, `returned`, `remaining`, `total`, provider, and the full fingerprint.

Audit the entire manifest before approval: start at offset zero, inspect/save each page, then add `returned` to the offset until `remaining` is zero. The two commands above cover all 101 items of a 101-item batch. Ordering follows the immutable manifest order, including already delivered items; it does not shift as pending counts shrink. Check the same batch ID, fingerprint, and total on every page. Use an isolated consistent backup or stop writers for a consistent audit of delivery status as well. No SQL is required to inspect all items. An offset at or beyond the end returns an empty page.

There is no automatic price approval. The SHA-256 fingerprint covers the feed/provider, stable product IDs, and old/target amount, currency, and formatted price. A replaced candidate requires its new fingerprint. Only one approved or paused batch may be active per feed. One current candidate and at most ten terminal batches per feed are retained; archive evidence separately if longer history is needed.

Approval does not send immediately. Subsequent valid scans drain the approved manifest, recording a generation-scoped claim immediately before each send and committing each accepted target snapshot with its delivered-item status. A callback must present the still-open matching claim; stale generations, missing reservations, and duplicate acknowledgements are rejected without mutation. A revoke blocks new claims but does not invalidate an already-open claim, so a successful in-flight delivery can still be acknowledged. An interrupted open claim may be reclaimed after restart with a new generation. This is intentionally at-least-once delivery: a crash after an external send and before acknowledgement can duplicate it. Pending items rotate by attempts/age. The intended 101-change recovery result is ten successful attempts leaving 91 pending under the **same** approval and fingerprint. Gate-2 real-monitor evidence for that scenario is being remediated; CLI pagination coverage alone does not establish monitor delivery correctness. Newly observed changes wait while that batch drains.

Every drain validates all pending targets against the current source and their prior persisted snapshots. Missing products, moved targets, inconsistent prior snapshots, or a hard invalid catalog pause recovery. A paused batch blocks normal processing; already delivered items stay delivered. Do not repeatedly approve a moved target expecting the safety check to disappear. Investigate, then either reapprove the exact paused fingerprint when its conditions are valid again, or revoke and inspect the next candidate:

```sh
python -m rss2discord.admin --database data/state.db price revoke 42 --fingerprint FULL_FINGERPRINT --reason "Target moved; reconcile remaining items"
python -m rss2discord.admin --database data/state.db price list --feed-id ddstore --all
```

Revocation also accepts `price revoke --feed-id ddstore --fingerprint FULL_FINGERPRINT --reason "Withdraw reviewed batch"`. Fingerprint lookup considers only candidate/approved/paused batches; recurring terminal fingerprints are audit history, not revocable work. It does not undo deliveries or reset snapshots. Terminal batches cannot be revoked. In-flight claim receipts are retained during bounded terminal pruning until their callback is acknowledged or released. Do not delete `price_snapshots` to acknowledge repricing: that destroys the comparison baseline instead of recovering it.

Normal, unapproved price delivery uses a persisted per-feed cursor. Selection is a short `BEGIN IMMEDIATE` transaction: an approved/paused batch blocks the scan, an obsolete candidate is revoked with `ChangeSetNoLongerRequiresApproval`, and the next sorted IDs after the cursor are saved before confirmation (wrapping once, at most ten IDs). Network confirmation and sends occur after commit; no database transaction spans that I/O. Empty selections still perform the recovery gate and permit a zero-change silent scan. The cursor survives process restarts and prevents the same early IDs from starving later IDs.

CCCenter additionally detail-confirms selected changes: identity, scalar price, currency, and amount must agree with the listing. Variable/range/unpriced or mismatched products are deferred. Up to ten selected detail confirmations share one aggregate 300-second, 36-request, 24-MiB budget, rather than ten independent deadlines. Catalog enumeration has its own bounded scan budget. Setec also checks detail/variant price agreement; distinct product IDs with matching titles are not duplicates.

## CCCenter discovery cutover

CCCenter enumerates validated listing pages (up to twelve) and does not invent publication timestamps. Discovery baseline approval is separate from price approval:

- A fresh feed automatically records its first complete nonempty inventory as an explicit silent baseline.
- An already initialized feed without a complete baseline records a candidate and enters `recovery_required`. It suppresses discovery delivery until that candidate is explicitly approved, avoiding a burst from newly visible catalog pages.
- Approval means **suppress this existing inventory**, not “deliver these items.” Baseline IDs are stored separately from legacy delivery rows; delivery counts remain delivery-only. Later unseen undated CCCenter IDs become eligible. Dated entries still obey the age filter, and strict RSS handling of missing timestamps is unchanged.

```sh
python -m rss2discord.admin --database data/state.db baseline list --feed-id cccenter
python -m rss2discord.admin --database data/state.db baseline show cccenter --offset 0 --sample-limit 100
python -m rss2discord.admin --database data/state.db baseline show cccenter --offset 100 --sample-limit 100
python -m rss2discord.admin --database data/state.db baseline approve --feed-id cccenter --fingerprint FULL_FINGERPRINT --reason "Reviewed complete cutover inventory; suppress existing IDs"
```

Review every `entry_ids` page using the same offset/limit and metadata rules as price inspection; the two commands above cover all 153 IDs of a 153-item baseline. IDs are sorted, and `entry_count` remains the full count. Baselines identify the feed but do not persist its provider, so `provider` is null. Require the same feed, full fingerprint, and total across pages; if a running scan replaces the candidate, restart the audit. Only a validated complete inventory creates a candidate; failed or incomplete scans do not qualify. If inventory changes before approval, inspect again and use the current fingerprint. Approving the same baseline fingerprint again is idempotent: it preserves the original approval timestamp and reason rather than rewriting audit history. There is no baseline-revoke CLI command.

## Health, cooldowns, and scheduling

```sh
python -m rss2discord.admin --database data/state.db health list --feed-id cccenter
```

Health rows are keyed by feed and `job_kind` (`ordinary`/`price`). States include `healthy`, `empty`, `failed`, `blocked`, `quarantined`, and `recovery_required`. Output includes a sanitized cause, `attempted_at`, `last_success_at`, `last_nonempty_at`, `transition_at`, `blocked_until`, `last_notified_at`, failure/attempt/success/nonempty/item counters, current `item_count`, `duration_ms`, and `scheduler_lag_ms`. Timestamps are Unix seconds; unknown historical timestamps remain null. Fetch health is not a Discord delivery receipt. Quiet successful feeds need not produce messages, and scheduler timing updates cannot clear quarantine.

Access challenges/HTTP 403 establish a persistent **six-hour** cooldown checked before ordinary and price fetching, including configured feeds sharing the same URL origin. Skips do not extend it. A real failed probe after expiry establishes another six-hour window; a successful probe records recovery. Origin sharing uses configured URLs, not inferred aliases or redirect destinations. State transitions/recovery log immediately; repeated unhealthy reminders are spaced six hours apart. There is no admin force-probe/clear-cooldown command. Shared price adapters preserve blocked health and cooldowns when a challenge also pauses an active batch; other batch failures can report `recovery_required`.

The scheduler invokes individual ordinary feeds and price jobs serially and adds no worker threads itself. It selects the earliest eligible deadline with rotating ties, advances from a fixed phase, and skips missed slots arithmetically rather than replaying a backlog. The global ordinary-feed gap (`delay_between_feeds`, default 61 seconds) starts after an ordinary job finishes; price jobs can run during that gap. Long synchronous scans still delay other jobs. A 300-second refresh interval is **not a promise of one scan every five minutes**. Use duration and lag to assess actual service cadence.

Implementation exception: Gjirafa50 still defaults to its pre-existing background price worker. Scheduler duration measures dispatch, not the full background scan. The worker persists its own sanitized failure/blocked health using a worker-owned database connection and rechecks its feed and configured same-origin peers' cooldowns after acquiring the provider lock. A queued-worker regression verifies that a peer challenge prevents the next worker from fetching. Do not infer live scan completion solely from scheduler timing.

## Limits and rollback

- Reklama5's observed Cloudflare challenge still requires upstream-approved access. Classification and cooldown do not restore access or bypass the challenge.
- Gjirafa50 `.mk` enumeration remains an unresolved provider contract. Expanded `.com` bounds have fixture evidence, not proof that a live complete scan fits the request/byte/time budgets. Completeness failures remain fail-closed.
- Delivery is at-least-once, not exactly-once: an ambiguous webhook timeout or a crash after Discord accepts but before persistence can cause duplicates. A batch stores exact targets and progress, but reconstructs message payloads; there is no full event outbox.
- Normal unapproved price transitions can coalesce: an unsent A→B may become A→C before the next scan. Approved manifests retain their exact pending targets and pause on movement instead.

For rollback, stop the service, preserve a new consistent state backup and batch audit, and disable affected price monitors (`price_check_interval: null`) before starting older code. Also remove/disable affected discovery feeds such as CCCenter if the older runtime cannot honor the explicit baseline. Older code can ignore recovery tables and their protections even if it can open the schema. Reconcile pending/paused/delivered batch items and ambiguous Discord outcomes before re-enabling anything. Restoring an older database can forget already-sent messages and cause replay; do not treat restoration, revocation, or row deletion as an undo of external deliveries.
