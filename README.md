# RSS2Discord

Forward RSS feeds and XenForo forum threads to Discord webhooks with clean embeds.

## Setup

```bash
git clone https://github.com/Delemangi/rss2discord.git
cd rss2discord
mkdir config
cp config.example.yaml config/config.yaml
# Edit config/config.yaml with your feeds and webhooks
docker compose up -d
```

## Configuration

```yaml
refresh_interval: 300

feeds:
    # RSS Feed Example
    - name: "My Feed"
      url: "https://example.com/feed.xml"
      webhook: "https://discord.com/api/webhooks/ID/TOKEN"
      strategy: "rss" # Optional, defaults to 'rss'
      webhook_name: "RSS Bot" # optional
      webhook_avatar: "https://url" # optional

    # XenForo Forum Thread Example
    - name: "Forum Thread"
      url: "https://forum.example.com/threads/topic.12345/"
      webhook: "https://discord.com/api/webhooks/ID/TOKEN"
      strategy: "xenforo" # Required for forum scraping
      webhook_name: "Forum Bot" # optional
      embed_color: 3447003 # optional
```

## Get Discord Webhook

1. Right-click Discord channel -> Edit Channel
2. Integrations -> Webhooks -> New Webhook
3. Copy webhook URL

## Features

- Monitor multiple RSS/Atom feeds
- Scrape XenForo forum threads for new posts
- Strategy-based architecture for easy extensibility
- Clean HTML/markup from descriptions
- Rich Discord embeds
- Custom webhook names & avatars
- Persistent state (no duplicates)
- Configurable refresh interval

## Scraping Strategies

### RSS Strategy (default)

- Supports RSS 2.0 and Atom feeds
- Automatically parses feed metadata
- Cleans HTML from descriptions

### XenForo Strategy

- Scrapes XenForo forum threads
- Monitors new posts in specified threads
- Extracts post content and author information

## License

This project is licensed under the terms of the MIT license.
