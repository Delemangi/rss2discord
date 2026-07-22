# RSS2Discord

Forward RSS/Atom feeds, XenForo thread posts, and IT.mk Oglasnik listings to Discord webhooks.

## What it supports

- RSS and Atom feeds, including public GitHub release feeds
- Optional RSS adapters for Hacker News and Reddit
- XenForo forum threads
- IT.mk Oglasnik index and category pages
- SQLite delivery history so entries are not posted twice
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

Edit `config/config.yaml`. Each feed needs a stable, unique `id`; changing it later makes old entries eligible for reposting.

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
```

Useful options:

| Key | Notes |
| --- | --- |
| `strategy` | `rss` by default; also supports `xenforo` and `itmk_oglasnik`. |
| `adapter` | Optional for RSS only: `hackernews` or `reddit`. |
| `max_post_age_days` | Set to `0` to disable age filtering. |
| `delay_between_feeds` | Increase if a source rate-limits requests. |
| `embed_color` | Components v2 accent color; key name is kept for compatibility. |

See `config/config.example.yaml` for the fully annotated configuration.

## Runtime notes

- Delivery state is stored in `data/state.db` as `(feed_id, entry_id)`.
- The database is created automatically on first startup.
- RSS and IT.mk responses are capped at 1 MiB and transient fetch failures are retried.
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
