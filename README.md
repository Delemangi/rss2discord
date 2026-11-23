# RSS2Discord

Monitor feeds, forums, marketplaces, and product catalogs; send new-item and optional price-change notifications to Discord.

## Supported sources

- RSS/Atom (including GitHub releases and GitLab commits), Hacker News and Reddit adapters
- XenForo and IT.mk Oglasnik
- Pazar3 and Reklama5 listings
- Ananas, Anhoch, CCCenter, DDStore, Gjirafa50, Hivetec, Neksio, Neptun, PROXNet, Setec, Technomarket, Wolfinet, and any public WooCommerce Store API shop
- Hetzner Auction and Hetzner Cloud offers, including Cloud availability changes
- OpenRouter model catalogs and price drops
- Time.mk aggregated Macedonian news with configurable category subsets and Open Graph summaries/images
- Fakulteti.mk student news from a configurable education/science/technology/career category subset
- EMSC earthquakes, Open-Meteo air quality, and tweets through a Nitter RSS profile

See [`config/config.example.yaml`](config/config.example.yaml) for provider URLs, examples, and configuration options. Some discovery feeds cover only recent products and can miss additions between checks. The Hacker News adapter adds points, comments, domain, front-page rank, the top comment for link posts, and Ask HN text. The Fakulteti.mk provider reads its public JSON API; the root URL fetches all posts, while `/category/<slug>,<slug>` restricts the feed to a category subset. The `tweets` strategy reads a Nitter RSS profile feed, keeps original tweets only (no replies, retweets, or pinned tweets), and rewrites links to x.com using the handle in each status link. The `emsc` strategy reads an EMSC FDSN event query and posts earthquake alerts with magnitude, depth, region, and an event link. The `arctic_shift` strategy reads one subreddit's `/new` listing through the public Arctic Shift archive; no OAuth credentials are required. It fetches at most 300 newest posts, delivers them oldest-first, and skips stickied, NSFW, spoiler, removed, and deleted posts. Use `arctic_shift_nsfw` for adult subreddits whose posts are all `over_18`; spoilers and stickied/pinned posts are still skipped. The legacy `reddit` strategy still uses the official Data API for already-approved OAuth apps; Reddit closed new API access and discontinued RSS in November 2026. Price scans send individual cards for up to 100 changes per feed by default; set `price_inline_limit` to raise or lower that threshold. Gjirafa50 price scans are best-effort against a live search index; products added or moved during a scan may be missed. Reklama5 access can be blocked by upstream challenges. Ananas uses a public Algolia proxy, requires `ANANAS_ALGOLIA_SEARCH_KEY` in the environment, and supports any category URL under `https://ananas.mk/kategorii/`; the category filter is resolved from the live category tree. Its full scans are best-effort against a live marketplace index and require at least 95% inventory coverage.

## Setec public-webstore scope

Setec searches now consistently select `status = 'published' AND is_web_active = 'true'` for latest discovery, full price-index counts/bands, and product-ID confirmation lookups. This is an explicitly reviewed source-scope change, separate from repairing public search-configuration discovery. Product IDs, variant handling, currency rules, price caps, and scheduling are unchanged.

Historical identities and price snapshots remain preserved, including products no longer visible in the public webstore; absence is not permission to reset or delete their history. Setec confirms product details before reporting a change and rotates through at most 2,000 confirmations per scan. Confirmed changes are reported, never silently dropped.

Search configuration is discovered afresh per operation from explicitly advertised same-origin public JavaScript, without a legacy-key fallback. Discovery is limited to 40 unique assets, 48 physical requests, 2 MiB per response, 16 MiB total decoded bytes, and 120 seconds. These are sublimits within full-catalog operations' existing 1,000-request/500-MiB caps and new 300-second deadline. Latest operations have a 150-second deadline and an enclosing discovery-plus-search byte allowance; their existing 1-MiB single-search-response and redirect-chain byte limits are unchanged. Full indexes are accepted only after a final root count/bounds recheck agrees with the initial summary; incomplete or drifting evidence fails closed.

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

Provider packages own source strategies, catalog access, parsing, and price monitors; the transports package holds shared request mechanics, including the shared listing HTTP path. The provider registry composes discovery strategies and price monitors from one set of provider names and labels. `app.py` wires adapters, strategies, and the scheduler; `discovery.py` owns the per-feed baseline, dedupe, enrichment, delivery, and health flow. The runtime and scheduler handle generic jobs, cadence, shutdown, and health reporting. Persistence and administrative workflows remain separate from that composition path.

See [price-monitoring flow](docs/price-monitoring-flow.md) for the comparison, planning, and delivery boundaries and their regression tests.

RSS/Atom responses are bounded to 1 MiB by default. A 4 MiB allowance applies only when both the requested URL and final response URL are exact public GitHub release Atom addresses (`https://github.com/<owner>/<repo>/releases.atom`); the same decoded-response size checks still apply.

## Price notifications

Each price scan compares the validated catalog with the stored baseline.

- Up to `price_inline_limit` changed products (100 by default) in one scan send one price-change card per product.
- A larger scan sends one compact brief: direction totals, up to five largest drops and five largest rises with links and percentages, and the complete searchable/sortable HTML report plus CSV attached.
- The same limit applies when pending notifications are drained, so a retried group at or below the limit sends cards instead of the brief. CCCenter confirms at most 100 changes per scan, so `price_inline_limit` values above 100 do not increase its card count.
- The baseline and the pending notification rows are written in one transaction before delivery. A failed or interrupted Discord send leaves the notification pending and retries on a later scan; changes are not lost.
- Delivery is at-least-once. An ambiguous timeout or crash after Discord accepts a message can produce a duplicate.
- A group that fails six consecutive delivery attempts is dead-lettered. It leaves the pending queue so later alerts can drain, its payload is cleared, and it remains inspectable with `notifications list --status dead_lettered`; the health summary reports the count.
- There is no approval, quarantine, or pause step for price changes. Invalid or incomplete catalog evidence is rejected and retried; it never overwrites a valid price.

## New-item notifications

- Up to `digest_threshold` new items (100 by default) in one scan send one card per item.
- More send one digest with up to three cards and complete searchable HTML and CSV attachments.
- Entries are marked delivered only after Discord confirms delivery, so a failed send retries on the next scan.
- There are no new-entry or delivery-history caps that can permanently stop a feed.

Upgrading from a database that still contains the retired approval, hold, or reconciliation tables drops those tables after a one-time cursor migration. Take a consistent backup before the first start with the new version. Price snapshots, delivered entries, initialized feeds, health, and pending notifications are preserved.

## Inspecting state

The admin CLI uses `data/state.db` by default, or `STATE_DB_PATH`; put `--database` before the command. All commands are read-only:

```sh
python -m rss2discord.admin --database data/state.db health list
python -m rss2discord.admin --database data/state.db notifications list --status pending --limit 50
python -m rss2discord.admin --database data/state.db summary
python -m rss2discord.admin --database data/state.db summary --json
```

Before any offline database operation, stop every writer and back up the database safely, including WAL/SHM sidecars; do not delete a WAL or the retained `.writer.lock` file.

## Maintenance and retries

`state_retention_days` prunes delivered entries, sent or dead-lettered price notifications, all but each product's newest price-history row, and price snapshots not observed within the window at startup and once per day while the process runs. The startup pass vacuums when rows were removed; periodic passes prune without a runtime VACUUM. A retention failure is logged and startup continues. Use `0` to disable retention. When `max_post_age_days` is `0` (no age filter), retention must also be `0`; otherwise retention must be at least `max_post_age_days`. Configuration validation enforces that pairing. Startup also removes health rows for feeds no longer in the configuration.

Fetch jobs retry up to three times and honor `Retry-After` up to 300 seconds. Transient SQLite busy/locked errors retry up to 12 times at 5-second intervals; after that the job fails and the scheduler continues with the remaining feeds. Optional daily health reporting is configured with `health_webhook` and `health_summary_interval`.

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
