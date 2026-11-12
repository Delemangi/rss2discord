# RSS2Discord

Monitor feeds, forums, marketplaces, and product catalogs; send new-item and optional price-change notifications to Discord.

## Supported sources

- RSS/Atom (including GitHub releases and GitLab commits), Hacker News and Reddit adapters
- XenForo and IT.mk Oglasnik
- Pazar3 and Reklama5 listings
- Anhoch, CCCenter, DDStore, Gjirafa50, Hivetec, Neksio, Neptun, Setec, and Technomarket products

See [`config/config.example.yaml`](config/config.example.yaml) for provider URLs, examples, and configuration options. Some discovery feeds cover only recent products and can miss additions between checks. Complete Gjirafa50 price enumeration remains unresolved on both storefronts; Reklama5 access can be blocked by upstream challenges.

## Setec public-webstore scope

Setec searches now consistently select `status = 'published' AND is_web_active = 'true'` for latest discovery, full price-index counts/bands, and product-ID confirmation lookups. This is an explicitly reviewed source-scope change, separate from repairing public search-configuration discovery. Product IDs, variant handling, currency rules, price caps, and scheduling are unchanged.

Historical identities and price snapshots remain preserved, including products no longer visible in the public webstore; absence is not permission to reset or delete their history. Setec confirms product details before reporting a change and rotates through at most 2,000 confirmations per scan. Confirmed changes are reported, never silently dropped.

Search configuration is discovered afresh per operation from explicitly advertised same-origin public JavaScript, without a legacy-key fallback. Discovery is limited to 40 unique assets, 48 physical requests, 2 MiB per response, 16 MiB total decoded bytes, and 120 seconds. These are sublimits within full-catalog operations' existing 1,000-request/500-MiB caps and new 300-second deadline. Latest operations have a 150-second deadline and an enclosing discovery-plus-search byte allowance; their existing 1-MiB single-search-response and redirect-chain byte limits are unchanged. Full indexes are accepted only after a final root count/bounds recheck agrees with the initial summary; incomplete or drifting evidence fails closed.

## Gjirafa50 observation-only rolling mode

An opt-in [rolling observation CLI](docs/gjirafa-observations.md) checks an explicitly supplied known-product manifest without requiring a stable full-catalog scan. It uses separate durable state, reports unknown coverage, and never creates production price baselines or sends alerts. Full-catalog Gjirafa50 pricing remains unchanged and disabled unless separately configured; observation results do not authorize enabling it.

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

## Code boundaries

Provider packages own source strategies, catalog access, parsing, and price monitors; the transports package holds shared request mechanics. Price-monitor builders compose provider monitors and dependencies, while the runtime and scheduler handle generic jobs, cadence, shutdown, and health reporting. Persistence and administrative workflows remain separate from that composition path.

See [price-monitoring flow](docs/price-monitoring-flow.md) for the comparison, planning, and delivery boundaries and their regression tests.

RSS/Atom responses are bounded to 1 MiB by default. A 2 MiB allowance applies only when both the requested URL and final response URL are exact public GitHub release Atom addresses (`https://github.com/<owner>/<repo>/releases.atom`); the same decoded-response size checks still apply.

## Price notifications

Each price scan compares the validated catalog with the stored baseline.

- Up to 25 changed products in one scan send one price-change card per product.
- A larger scan sends one digest message with a few example cards and two attachments: a complete Markdown table and a complete CSV.
- The baseline and the pending notification rows are written in one transaction before delivery. A failed or interrupted Discord send leaves the notification pending and retries on a later scan; changes are not lost.
- Delivery is at-least-once. An ambiguous timeout or crash after Discord accepts a message can produce a duplicate.
- There is no approval, quarantine, or pause step for price changes. Invalid or incomplete catalog evidence is rejected and retried; it never overwrites a valid price.

Upgrading from a database that still contains the retired approval, hold, or reconciliation tables drops those tables after a one-time cursor migration. Take a consistent backup before the first start with the new version. Price snapshots, delivered entries, initialized feeds, health, and pending notifications are preserved.

## Inspecting state

The admin CLI uses `data/state.db` by default, or `STATE_DB_PATH`; put `--database` before the command. Both commands are read-only:

```sh
python -m rss2discord.admin --database data/state.db health list
python -m rss2discord.admin --database data/state.db notifications list --status pending --limit 50
```

Before any offline database operation, stop every writer and back up the database safely, including WAL/SHM sidecars; do not delete a WAL or the retained `.writer.lock` file.

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
