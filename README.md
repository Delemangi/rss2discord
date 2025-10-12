# RSS2Discord

Forward RSS feeds to Discord webhooks with clean embeds.

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
    - name: "My Feed"
      url: "https://example.com/feed.xml"
      webhook: "https://discord.com/api/webhooks/ID/TOKEN"
      webhook_name: "RSS Bot" # optional
      webhook_avatar: "https://url" # optional
```

## Get Discord Webhook

1. Right-click Discord channel -> Edit Channel
2. Integrations -> Webhooks -> New Webhook
3. Copy webhook URL

## Features

- Monitor multiple RSS/Atom feeds
- Clean HTML/markup from descriptions
- Rich Discord embeds
- Custom webhook names & avatars
- Persistent state (no duplicates)
- Configurable refresh interval

## License

This project is licensed under the terms of the MIT license.
