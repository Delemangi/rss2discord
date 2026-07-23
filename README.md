# RSS2Discord

Forward RSS/Atom feeds, XenForo thread posts, IT.mk Oglasnik listings, and Anhoch product updates to Discord webhooks.

## What it supports

- RSS and Atom feeds, including public GitHub release feeds
- Optional RSS adapters for Hacker News and Reddit
- XenForo forum threads
- IT.mk Oglasnik index and category pages
- New products from the latest Anhoch catalog pages and opt-in selling-price alerts
- SQLite delivery history and persistent Anhoch selling-price snapshots
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
  webhook_avatar: "https://www.anhoch.com/storage/media/lUuXIR1al8ZZVSTbX4e7Rryi6jgaymSLQGsDYjkT.svg"
```

`price_check_interval: 3600` opts an Anhoch feed into an immediate, silent full-catalog selling-price baseline and then hourly price checks. To enable it in a Compose deployment, add that line beneath the Anhoch feed in the active `/app/config/config.yaml`; do not put it on a non-Anhoch feed. Omit the key or set it to `null` to disable price monitoring.

Useful options:

| Key | Notes |
| --- | --- |
| `strategy` | `rss` by default; also supports `xenforo`, `itmk_oglasnik`, and `anhoch`. |
| `adapter` | Optional for RSS only: `hackernews` or `reddit`. |
| `max_post_age_days` | Set to `0` to disable age filtering. |
| `delay_between_feeds` | Increase if a source rate-limits requests. |
| `embed_color` | Components v2 accent color; key name is kept for compatibility. |
| `price_check_interval` | Anhoch only. Set to `3600` for hourly full-catalog selling-price checks; omit or set to `null` to disable. |

See `config/config.example.yaml` for the fully annotated configuration.

## Runtime notes

- Delivery state is stored in `data/state.db` as `(feed_id, entry_id)`.
- Anhoch selling-price snapshots are stored persistently in the same SQLite database by feed and product.
- The database is created automatically on first startup.
- RSS, IT.mk, and ordinary Anhoch new-product responses are capped at 1 MiB and transient fetch failures are retried.
- Anhoch new-product checks follow `refresh_interval` (300 seconds by default), inspect at most the latest 90 products, and seed the first successful fetch without notifications.
- Enabled Anhoch price scans run immediately and then at `price_check_interval`; the initial price snapshot is silent. Full-catalog scans request 500 products per page, cap each response at 2 MiB, and allow up to 100 bounded pages (200 MiB total).
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
