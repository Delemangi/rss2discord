# RSS2Discord

Monitor feeds, forums, marketplaces, and product catalogs; send new-item and optional price-change notifications to Discord.

## Supported sources

- RSS/Atom (including GitHub releases and GitLab commits), Hacker News and Reddit adapters
- XenForo and IT.mk Oglasnik
- Pazar3 and Reklama5 listings
- Anhoch, CCCenter, DDStore, Gjirafa50, Hivetec, Neksio, Neptun, Setec, and Technomarket products

See [`config/config.example.yaml`](config/config.example.yaml) for provider URLs, examples, and configuration options. Some discovery feeds cover only recent products and can miss additions between checks. Complete Gjirafa50 price enumeration remains unresolved on both storefronts; Reklama5 access can be blocked by upstream challenges.

## Docker quick start

```sh
git clone https://github.com/Delemangi/rss2discord.git
cd rss2discord
mkdir -p config data
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml: remove unused examples and set real webhook URLs.
sudo chown -R 10001:10001 data  # Usually unnecessary with Docker Desktop.
docker compose up -d --build   # Build from this checkout.
```

To use the published image instead, run `docker compose -f compose.prod.yaml up -d`. Follow logs with `docker compose logs -f rss2discord`; stop with `docker compose down`.

## Configuration

Start with this minimal RSS feed, then use the [annotated configuration](config/config.example.yaml) for all provider examples and options:

```yaml
refresh_interval: 300
delay_between_feeds: 0
delay_between_posts: 2
max_post_age_days: 7
feeds:
  - id: my-feed                 # Stable unique ID; changing it can repost old items.
    name: My Feed
    url: https://example.com/feed.xml
    webhook: https://discord.com/api/webhooks/ID/TOKEN
    strategy: rss
```

`refresh_interval` is the ordinary discovery fallback; a feed's optional `ordinary_check_interval` overrides it without changing price scans. `price_check_interval` independently enables an immediate price scan and later periodic scans; its first scan silently establishes a baseline. `delay_between_feeds` spaces ordinary jobs, not individual provider requests; `delay_between_posts` spaces Discord posts. Pazar3 requests also share a 20-second host-wide pacer. Scheduling is best-effort, not a promise of exact scan times. Keep feed IDs stable. Treat webhook URLs as secrets.

## Inspecting and recovering state

The admin CLI uses `data/state.db` by default, or `STATE_DB_PATH`; put `--database` before the command. Health and price/baseline inspection commands are read-only. Replace example batch `42`, feed IDs, and fingerprints with inspected values. Before a state-changing operation, stop every writer and back up the database safely, including any WAL/SHM sidecars; do not delete a WAL or the retained `.writer.lock` file.

```sh
python -m rss2discord.admin --database data/state.db health list
python -m rss2discord.admin --database data/state.db price list --feed-id FEED_ID --all
python -m rss2discord.admin --database data/state.db price show 42 --offset 0 --sample-limit 100
python -m rss2discord.admin --database data/state.db baseline list --feed-id cccenter
python -m rss2discord.admin --database data/state.db baseline show cccenter --offset 0 --sample-limit 100
```

For `price show`, inspect every page (`--offset` advances by the returned count) and confirm the same full fingerprint, batch, and total throughout. More than 100 changed prices require manual approval; normal and approved scans allow at most ten delivery attempts, not ten guaranteed deliveries. Approval permits later scans to deliver; it does not send immediately. Revocation prevents new deliveries but cannot stop an already in-flight send or undo one already sent. Both approval and revocation require the full fingerprint and a meaningful reason:

```sh
python -m rss2discord.admin --database data/state.db price approve --feed-id FEED_ID --fingerprint FULL_FINGERPRINT --reason "Reviewed exact targets"
python -m rss2discord.admin --database data/state.db price revoke 42 --fingerprint FULL_FINGERPRINT --reason "Withdraw review"
```

CCCenter baseline approval suppresses the reviewed existing inventory; it does not notify for those products. Inspect every page and verify its full fingerprint and count before approval. There is no baseline-revoke command:

```sh
python -m rss2discord.admin --database data/state.db baseline approve --feed-id cccenter --fingerprint FULL_FINGERPRINT --reason "Suppress reviewed inventory"
```

## DDStore / Hivetec future-only reconciliation

This offline workflow applies only to DDStore and Hivetec. It silently accepts reviewed **current** prices as the future baseline; it does not send historical alerts. Price holds do not suppress product discovery. New V2 plans distinguish review-only `hold` from availability `defer`; V1 plans and existing holds remain review-only. There are no automatic price-legitimacy thresholds or commands to release review holds.

1. Stop **all** writers, including old binaries and database clients, and verify a consistent backup. Keep them stopped through apply and any discovery-baseline approval. `--writers-stopped` is an operator acknowledgment, not an automated check. Do not bypass `.writer.lock` or clear claims to proceed.
2. Use read-only `price list` / `price show` to select the feed, batch, and exact full batch fingerprint. Create a fresh plan with a truthful operator and review reference:

   ```sh
   python -m rss2discord.admin --database data/state.db reconcile plan --config config/config.yaml --feed-id ddstore --batch-id 42 --batch-fingerprint FULL_BATCH_FINGERPRINT --reason "Operator: YOUR_NAME; review ticket: YOUR_REFERENCE" --writers-stopped --output draft.json
   ```

3. Preserve the draft and edit a separate copy. Review **every** item and its context. `accept` adopts the exact positive MKD price; `hold` preserves the old snapshot and requires review indefinitely; `defer` is only for missing/unpriced products and permits a later silent reset when a complete valid catalog supplies a price; `noop` is only for unchanged prices. Missing/unpriced products require `hold` or V2 `defer`. Existing hold kinds cannot be downgraded. Active recovery batches or open claims can delay deferred products' return; their release and replacement baseline are recorded atomically without sending.
4. Seal the edited artifact, independently inspect it and retain the full reconciliation fingerprint, then apply and inspect the receipt and holds:

   ```sh
   python -m rss2discord.admin reconcile review --plan edited.json --output reviewed.json
   python -m rss2discord.admin --database data/state.db reconcile apply --config config/config.yaml --feed-id ddstore --plan reviewed.json --fingerprint FULL_RECONCILIATION_FINGERPRINT --writers-stopped
   python -m rss2discord.admin --database data/state.db reconcile receipt --fingerprint FULL_RECONCILIATION_FINGERPRINT
    python -m rss2discord.admin --database data/state.db reconcile holds --feed-id ddstore
    ```

5. For future-only **discovery** too, separately prepare the known inventory from the applied receipt, inspect every page with `baseline show`, and approve its exact baseline fingerprint before restarting writers. This suppresses historical accepted, held, missing and unpriced identities without marking them delivered. An incompatible complete ordinary baseline is immutable: preparation refuses it; stop and review the mismatch rather than expecting replacement:

   ```sh
   python -m rss2discord.admin --database data/state.db baseline prepare --config config/config.yaml --feed-id ddstore --reconciliation-fingerprint FULL_RECONCILIATION_FINGERPRINT --writers-stopped --reason "Prepare reviewed historical inventory"
   python -m rss2discord.admin --database data/state.db baseline approve --feed-id ddstore --fingerprint FULL_BASELINE_FINGERPRINT --reason "Suppress reviewed historical inventory"
   ```

Plan output files must not already exist. Sealing validates an artifact; it is not a signature or authorization. Plans expire after 24 hours; each evidence operation is limited to 300 seconds. Apply refetches and rechecks complete source and database evidence. Drift, incomplete evidence, or invalid decisions cause refusal without database changes: regenerate and review a fresh plan; never force a mismatch. Retain the receipt, artifacts, and backup. Reconciliation records an audit receipt without marking historical alerts as delivered.

## Runtime and development

Container paths can be overridden with `CONFIG_PATH` (default `/app/config/config.yaml`) and `STATE_DB_PATH` (default `/app/data/state.db`). Delivery is at-least-once: an ambiguous Discord timeout or crash can cause a duplicate. There is no rollback command; do not delete database history or snapshots to undo a send.

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required for local development:

```sh
uv sync --frozen --dev
uv run pytest
uv run ruff check .
uv run mypy .
CONFIG_PATH=config/config.yaml STATE_DB_PATH=data/state.db uv run rss2discord
```

Create a webhook in Discord under **Channel Settings → Integrations → Webhooks**. Licensed under the MIT License; see [`LICENSE`](LICENSE).
