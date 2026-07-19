# RSS2Discord

Forward RSS/Atom entries and XenForo thread posts to Discord webhooks as embeds.

## Features

- RSS, Atom, and XenForo sources
- Stable per-feed delivery history in SQLite
- Immediate persistence after every successful Discord delivery
- Retry on the next poll when Discord delivery fails
- Configurable entry age, polling interval, embed color, name, and avatar
- Graceful shutdown during polling, rate-limit backoff, and post delays
- Non-root, read-only container deployment

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

The ownership command lets the container's fixed non-root user write the SQLite
database. Docker Desktop users may not need it. To view logs or stop the service:

```bash
docker compose logs -f rss2discord
docker compose down
```

For the published image, use `compose.prod.yaml`:

```bash
docker compose -f compose.prod.yaml up -d
```

## Configuration

Each feed requires a stable, unique `id`. It may contain lowercase letters,
numbers, periods, underscores, and hyphens. Do not change an ID after deployment:
delivery history is namespaced by it, so a changed ID makes existing entries
eligible for delivery again.

```yaml
refresh_interval: 300
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

  - id: "forum-thread"
    name: "Forum Thread"
    url: "https://forum.example.com/threads/topic.12345/"
    webhook: "https://discord.com/api/webhooks/ID/TOKEN"
    strategy: "xenforo"
```

`strategy` defaults to `rss`. Set `max_post_age_days` to `0` to disable age
filtering. When age filtering is enabled, entries without a valid timestamp are
skipped rather than assigned an invented timestamp.

See `config/config.example.yaml` for a fully annotated example.

## Delivery state

Delivery history is stored at `data/state.db`. The database uses `(feed_id,
entry_id)` as its key, so two configured feeds can use the same URL without
sharing delivery history.

The database is created automatically on first startup. When upgrading from a
release that did not use `state.db`, delivery history starts empty and previously
sent entries may be sent again.

## Runtime paths

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

Run the application with paths suitable for local development:

```bash
CONFIG_PATH=config/config.yaml \
STATE_DB_PATH=data/state.db \
uv run python main.py
```

## Get a Discord webhook

1. Right-click the target Discord channel and select **Edit Channel**.
2. Open **Integrations**, then **Webhooks**.
3. Create a webhook and copy its URL into the feed configuration.

## License

This project is licensed under the terms of the MIT license.
