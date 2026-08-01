# RSS2Discord

Forward RSS/Atom feeds, XenForo thread posts, IT.mk Oglasnik listings, and Anhoch, DDStore, Neksio, Neptun, or Setec product updates to Discord webhooks.

## What it supports

- RSS and Atom feeds, including public GitHub release feeds
- Optional RSS adapters for Hacker News and Reddit
- XenForo forum threads
- IT.mk Oglasnik index and category pages
- New products from Anhoch's catalog and opt-in selling-price alerts
- New products from Neksio's full public catalog and opt-in selling-price alerts
- New products from Setec's online catalog and opt-in selling-price alerts
- New products from DDStore's public GraphQL catalog and opt-in selling-price alerts
- New products from one Neptun category and opt-in actual-price alerts
- SQLite delivery history and persistent price snapshots for Anhoch, DDStore, Neksio, Neptun, and Setec
- Discord Components v2 messages with labels, links, categories, thumbnails, and text fallbacks

## Docker Compose setup

```bash
git clone https://github.com/Delemangi/rss2discord.git
cd rss2discord
mkdir -p config data
cp config/config.example.yaml config/config.yaml
# Edit config/config.yaml and replace the example feeds and webhook URLs.
sudo chown -R 10001:10001 data
docker compose up -d --build
```

Docker Desktop users may not need the `chown` step. View logs or stop the service with:

```bash
docker compose logs -f rss2discord
docker compose down
```

To run the published image instead of building locally:

```bash
docker compose -f compose.prod.yaml up -d
```

## Configuration

The checked-in `config/config.example.yaml` contains safe placeholders. Copy it to the ignored deployment configuration, then edit `config/config.yaml`; Compose mounts that active file read-only at `/app/config/config.yaml`. Each feed needs a stable, unique `id`; changing it later makes old entries eligible for reposting.

```yaml
refresh_interval: 300
delay_between_feeds: 0
delay_between_posts: 2
max_post_age_days: 7

feeds:
  - id: "my-feed"
    name: "My Feed"
    url: "https://example.com/feed.xml"
    webhook: "https://discord.com/api/webhooks/ID/TOKEN"
    strategy: "rss"
    webhook_name: "RSS Bot"
    webhook_avatar: "https://example.com/avatar.png"
    embed_color: 5814783
```

Common feed types:

```yaml
# Hacker News RSS with API enrichment
- id: "hacker-news"
  name: "Hacker News"
  url: "https://news.ycombinator.com/rss"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "rss"
  adapter: "hackernews"

# Reddit RSS without OAuth
- id: "reddit-python"
  name: "r/Python"
  url: "https://www.reddit.com/r/python/.rss"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "rss"
  adapter: "reddit"

# GitHub releases
- id: "github-cli-releases"
  name: "GitHub CLI Releases"
  url: "https://github.com/cli/cli/releases.atom"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "rss"

# XenForo thread
- id: "forum-thread"
  name: "Forum Thread"
  url: "https://forum.example.com/threads/topic.12345/"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "xenforo"

# IT.mk Oglasnik
- id: "itmk-oglasnik"
  name: "IT.mk Oglasnik"
  url: "https://forum.it.mk/oglasnik/"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "itmk_oglasnik"

# Anhoch new products and opt-in selling-price monitoring
- id: "anhoch-new-products"
  name: "Anhoch New Products"
  url: "https://www.anhoch.com/products?inStockOnly=2"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "anhoch"
  price_check_interval: 3600
  webhook_name: "Anhoch"
  webhook_avatar: "https://t3.gstatic.com/faviconV2?client=SOCIAL&type=FAVICON&fallback_opts=TYPE,SIZE,URL&url=https://www.anhoch.com&size=256"

# Neksio new products and opt-in selling-price monitoring
- id: "neksio-products"
  name: "Neksio Products"
  url: "https://g.store.neksio.mk/"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "neksio"
  price_check_interval: 3600
  webhook_name: "Neksio"

# Setec new products and opt-in selling-price monitoring
- id: "setec-new-products"
  name: "Setec New Products"
  url: "https://setec.mk/e-prodazba"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "setec"
  price_check_interval: 3600
  webhook_name: "Setec"

# DDStore new products and opt-in selling-price monitoring
- id: "ddstore-new-products"
  name: "DDStore New Products"
  url: "https://ddstore.mk/"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "ddstore"
  price_check_interval: 3600
  webhook_name: "DDStore"

# Neptun category products and opt-in actual-price monitoring
- id: "neptun-computers"
  name: "Neptun Computers"
  url: "https://www.neptun.mk/KOMPJUTERI.nspx"
  webhook: "https://discord.com/api/webhooks/ID/TOKEN"
  strategy: "neptun"
  price_check_interval: 3600
  webhook_name: "Neptun"
```

`price_check_interval: 3600` opts an Anhoch, DDStore, Neksio, Neptun, or Setec feed into an immediate, independent price scan. The first scan silently stores a baseline; later scans run at the configured interval. Neptun scans only the category named by its feed URL. Omit the key or set it to `null` to disable price monitoring.

Useful options:

| Key | Notes |
| --- | --- |
| `strategy` | `rss` by default; also supports `xenforo`, `itmk_oglasnik`, `anhoch`, `ddstore`, `neksio`, `neptun`, and `setec`. |
| `adapter` | Optional for RSS only: `hackernews` or `reddit`. |
| `max_post_age_days` | Set to `0` to disable age filtering. |
| `delay_between_feeds` | Increase if a source rate-limits requests. |
| `embed_color` | Components v2 accent color; key name is kept for compatibility. |
| `price_check_interval` | Anhoch, DDStore, Neksio, Neptun, or Setec only. Set to `3600` for hourly price checks; omit or set to `null` to disable. |

See `config/config.example.yaml` for the fully annotated configuration.

## Runtime notes

- Delivery state is stored in `data/state.db` as `(feed_id, entry_id)`.
- Selling-price snapshots are stored persistently in the same SQLite database by feed and product.
- The database is created automatically on first startup.
- RSS, IT.mk, ordinary Anhoch new-product, Setec, and Neksio first-party responses are capped at 1 MiB and transient fetch failures are retried. Neksio accepts only `https://g.store.neksio.mk/`, follows only same-origin redirects, and applies a 30-second request timeout. Anhoch price responses are capped at 2 MiB.
- IT.mk Oglasnik seeds the first successful fetch without notifications.
- Anhoch new-product checks follow `refresh_interval` (300 seconds by default), inspect at most the latest 90 products, and seed the first successful fetch without notifications.
- Neksio discovery fetches the full public catalog by enumerating homepage categories and their pages. It uses separate bounded first-party requests for the homepage and catalog pages, with up to 100 categories, 100 pages per category, 100 products per page, and 10,000 products total. This bounds request count and response cost, but a large catalog can still require many first-party requests. The first successful discovery seeds without notifications.
- Anhoch and Neksio new-product and price checks intentionally use separate catalog requests. Discovery retains its source-specific behavior, while price monitoring compares the complete catalog without coupling either job's failures to the other.
- Anhoch product images are downloaded with browser-compatible TLS and uploaded to Discord as Components v2 thumbnail attachments. Transient image failures are retried at most twice across redirects within one 30-second operation deadline, honoring `Retry-After` only when it fits within that deadline. If an image cannot be retrieved safely, the product update is delivered without a thumbnail.
- Setec checks at most the latest 30 products and seeds the first successful fetch without notifications.
- DDStore performs a bounded traversal of its public GraphQL catalog, then selects the latest 30 products by `created_at` and stable product UID in oldest-to-newest delivery order. The first successful fetch seeds without notifications. As a fail-closed integration policy, a zero GraphQL price is treated as unavailable and labeled `Ask for price` rather than displayed as free.
- Neptun accepts only credential-free, query-free, fragment-free HTTPS category URLs on exact host `neptun.mk` or `www.neptun.mk`; requests normalize to `https://www.neptun.mk` and redirects must remain on that origin. Discovery reads the category's embedded initial search model, requests exactly newest sort `7`, page 1, and 30 products, then delivers API-newest results oldest first. Each category/API response is capped at 5 MiB. The first successful non-empty window seeds without notifications, and delivery history is capped at 10,000 entries.
- Enabled Anhoch, DDStore, Neksio, Neptun, and Setec price scans run immediately and independently, then at `price_check_interval`; the initial price snapshot is silent. Anhoch full-catalog scans request 500 products per page, cap each response at 2 MiB, and allow up to 100 bounded pages (200 MiB total). DDStore scans request 500 products per page and allow at most 20,000 products across 40 pages, with a 2 MiB per-response cap, an 80 MiB total response cap, and a 300-second absolute scan bound across requests, redirects, transfers, and retry handling. DDStore also rejects products with more than 64 categories and retains at most 50,000 price snapshots per feed. Zero-valued unavailable prices do not consume snapshot capacity or replace the last real price. Full-catalog price retries share that one deadline and byte budget. Ordinary discovery uses the app's generic retry policy, where each retry is a separate fetch attempt with a fresh bounded scan budget. Neksio price scans use the same full-category bounds as discovery, with each response capped at 1 MiB. Setec full-catalog scans request 250 products per page, allow up to 100 pages (25,000 products), cap each response at 5 MiB, and allow 500 MiB total. Setec products without a current first-variant price are skipped without deleting prior snapshots.
- DDStore price monitoring delivers at most 100 changes from one scan. If 101 or more prices change together, the scan sends no alerts and advances no affected snapshots, so it retries against the same baseline later. The integration also applies a 50,000-entry discovery delivery-history safety limit per feed. A feed that reaches the price-change, snapshot, or delivery-history limit remains fail-closed. To reset a legitimate catalog-wide repricing or oversized snapshot history, stop the service and delete that feed's `price_snapshots` rows. To reset delivery history, stop the service and delete that feed's rows from both `delivered_entries` and `initialized_feeds` in one SQLite transaction; the next fetch will silently seed the current product window.
- Neptun price monitoring traverses only the configured category with 50 products per page, at most 100 pages / 5,000 products, a 5 MiB per-response cap, and a 500 MiB total cap. Retries restart at page one. Changed totals, incomplete traversal, oversized pages, and conflicting duplicate IDs fail closed. Only positive `ActualPrice` values are compared; unavailable values never replace a previous real snapshot. A feed retains at most 10,000 snapshots and delivers at most 100 changes per scan. Changed snapshots persist only after Discord accepts their alert.
- A Discord delivery is recorded immediately after Discord accepts the message.
- If a database write is interrupted after delivery, that entry may be posted again on the next startup.
- External feed mentions are not expanded in Discord messages.

Runtime paths:

| Environment variable | Container default | Purpose |
| --- | --- | --- |
| `CONFIG_PATH` | `/app/config/config.yaml` | YAML configuration |
| `STATE_DB_PATH` | `/app/data/state.db` | SQLite delivery ledger |

## Local development

Python 3.12 and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv sync --frozen --dev
uv run pytest
uv run ruff check .
uv run mypy .
```

Run locally:

```bash
CONFIG_PATH=config/config.yaml \
STATE_DB_PATH=data/state.db \
uv run rss2discord
```

## Discord webhook

In Discord, open the target channel settings, go to **Integrations** > **Webhooks**, create a webhook, and copy its URL into `config/config.yaml`.

## License

This project is licensed under the terms of the MIT license.
