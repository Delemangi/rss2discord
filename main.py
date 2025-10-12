import json
import logging
import os
import re
import sys
import time
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Any

import feedparser
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class RSSToDiscord:
    """Main application class for RSS to Discord webhook forwarding."""

    def __init__(self, config_path: str | None = None) -> None:
        """Initialize the application with configuration."""
        if config_path is None:
            config_path = os.environ.get("CONFIG_PATH", "config.yaml")
        self.config_path = Path(config_path)
        self.config = self._load_config()
        self.state_file = Path("state.json")
        self.state = self._load_state()

    def _load_config(self) -> dict[str, Any]:
        """Load configuration from YAML file."""
        try:
            with self.config_path.open() as f:
                config = yaml.safe_load(f)
        except FileNotFoundError:
            logger.exception("Configuration file not found: %s", self.config_path)
            sys.exit(1)
        except yaml.YAMLError:
            logger.exception("Error parsing YAML configuration")
            sys.exit(1)
        else:
            logger.info("Loaded configuration from %s", self.config_path)
            return config

    def _load_state(self) -> dict[str, Any]:
        """Load state from JSON file to track processed items."""
        if self.state_file.exists():
            try:
                with self.state_file.open() as f:
                    state = json.load(f)
            except json.JSONDecodeError:
                logger.warning("Error loading state file. Starting fresh.")
            else:
                logger.info("Loaded state from %s", self.state_file)
                return state
        return {"feeds": {}}

    def _save_state(self) -> None:
        """Save state to JSON file."""
        try:
            with self.state_file.open("w") as f:
                json.dump(self.state, f, indent=2)
            logger.debug("State saved successfully")
        except Exception:
            logger.exception("Error saving state")

    def _clean_description(self, text: str) -> str:
        """Clean HTML tags and unwanted content from description."""
        if not text:
            return ""

        text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", text)
        text = unescape(text)
        text = text.replace("&#32;", " ")
        text = re.sub(
            r"\s*submitted by\s*/u/\S+.*$",
            "",
            text,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        text = re.sub(r"\[link\]|\[comments\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\n{3,}", "\n\n", text)
        text = text.strip()

        return text

    def _send_webhook_request(
        self,
        webhook_url: str,
        payload: dict[str, Any],
        max_retries: int = 3,
    ) -> bool:
        """Send webhook request with retry logic for rate limiting."""
        base_delay = 2

        for attempt in range(max_retries):
            try:
                response = requests.post(
                    webhook_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                    timeout=10,
                )

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        wait_time = float(retry_after)
                    else:
                        wait_time = base_delay * (2**attempt)

                    logger.warning(
                        "Rate limited (429), waiting %s seconds before retry %d/%d",
                        wait_time,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()

            except requests.exceptions.Timeout:
                logger.warning(
                    "Timeout sending to Discord, retry %d/%d",
                    attempt + 1,
                    max_retries,
                )
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (2**attempt))
                    continue
                raise
            else:
                return True

        return False

    def _send_to_discord(
        self,
        webhook_url: str,
        entry: Any,  # noqa: ANN401
        feed_title: str,
        webhook_name: str | None = None,
        webhook_avatar: str | None = None,
        embed_color: int | None = None,
    ) -> bool:
        """Send an RSS entry to Discord webhook with retry logic."""
        try:
            title = entry.get("title", "No Title")
            link = entry.get("link", "")
            description = entry.get("summary", entry.get("description", ""))
            author = entry.get("author", "")

            title = unescape(title)
            description = self._clean_description(description)

            if len(description) > 2000:
                description = description[:1997] + "..."

            embed = {
                "title": title,
                "url": link,
                "description": description,
                "color": embed_color if embed_color is not None else 5814783,
                "timestamp": self._get_timestamp(entry),
                "footer": {"text": feed_title},
            }

            if author:
                embed["author"] = {"name": author}

            payload: dict[str, Any] = {"embeds": [embed]}

            if webhook_name:
                payload["username"] = webhook_name

            if webhook_avatar:
                payload["avatar_url"] = webhook_avatar

            success = self._send_webhook_request(webhook_url, payload)
            if success:
                logger.info("Sent to Discord: %s", title)
            else:
                logger.error("Failed to send to Discord: %s", title)

        except requests.exceptions.RequestException:
            logger.exception("Error sending to Discord webhook")
            return False
        except Exception:
            logger.exception("Unexpected error sending to Discord")
            return False
        else:
            return success

    def _get_timestamp(self, entry: Any) -> str:  # noqa: ANN401
        """Get ISO timestamp from entry."""
        try:
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                dt = datetime(*entry.published_parsed[:6], tzinfo=UTC)  # type: ignore[misc]
                return dt.isoformat()
            if hasattr(entry, "updated_parsed") and entry.updated_parsed:
                dt = datetime(*entry.updated_parsed[:6], tzinfo=UTC)  # type: ignore[misc]
                return dt.isoformat()
        except Exception:
            logger.debug("Could not parse timestamp from entry")
        return datetime.now(UTC).isoformat()

    def _get_entry_id(self, entry: Any) -> str:  # noqa: ANN401
        """Get unique identifier for an entry."""
        if hasattr(entry, "id"):
            return entry.id
        if hasattr(entry, "link"):
            return entry.link
        if hasattr(entry, "title"):
            return entry.title
        return str(hash(str(entry)))

    def _is_entry_too_old(self, entry: Any, max_age_days: int) -> bool:  # noqa: ANN401
        """Check if an entry is older than the maximum allowed age."""
        if max_age_days <= 0:
            return False

        try:
            entry_time = None
            entry_title = getattr(entry, "title", "Unknown")

            if hasattr(entry, "published_parsed") and entry.published_parsed:
                entry_time = datetime(*entry.published_parsed[:6], tzinfo=UTC)  # type: ignore[misc]
                logger.debug(
                    "Entry '%s' published: %s",
                    entry_title,
                    entry_time.isoformat(),
                )
            elif hasattr(entry, "updated_parsed") and entry.updated_parsed:
                entry_time = datetime(*entry.updated_parsed[:6], tzinfo=UTC)  # type: ignore[misc]
                logger.debug(
                    "Entry '%s' updated: %s",
                    entry_title,
                    entry_time.isoformat(),
                )

            if entry_time:
                current_time = datetime.now(UTC)
                age_days = (current_time - entry_time).days
                age_hours = (current_time - entry_time).total_seconds() / 3600
                is_old = age_days > max_age_days

                logger.info(
                    "Entry '%s' age: %.1f hours (%d days) - Max: %d days - Too old: %s",
                    entry_title,
                    age_hours,
                    age_days,
                    max_age_days,
                    is_old,
                )
                return is_old

        except Exception:
            logger.exception(
                "Error determining entry age, treating as too old: %s",
                getattr(entry, "title", "Unknown"),
            )
            return True

        logger.warning(
            "No timestamp found for entry, treating as too old: %s",
            getattr(entry, "title", "Unknown"),
        )
        return True

    def _filter_new_entries(
        self,
        entries: list[Any],
        processed_ids: list[str],
        max_age_days: int,
    ) -> tuple[list[tuple[str, Any]], int]:
        """Filter entries to find new ones that aren't too old."""
        new_entries = []
        skipped_old = 0

        for entry in reversed(entries):
            entry_id = self._get_entry_id(entry)

            if entry_id not in processed_ids:
                if self._is_entry_too_old(entry, max_age_days):
                    skipped_old += 1
                    processed_ids.append(entry_id)
                    if len(processed_ids) > 1000:
                        processed_ids.pop(0)
                else:
                    new_entries.append((entry_id, entry))

        return new_entries, skipped_old

    def _process_new_entries(
        self,
        new_entries: list[tuple[str, Any]],
        webhook_url: str,
        feed_title: str,
        processed_ids: list[str],
        webhook_name: str | None = None,
        webhook_avatar: str | None = None,
        embed_color: int | None = None,
    ) -> None:
        """Process and send new entries to Discord."""
        delay_between_posts = self.config.get("delay_between_posts", 2)

        for entry_id, entry in new_entries:
            if self._send_to_discord(
                webhook_url,
                entry,
                feed_title,
                webhook_name,
                webhook_avatar,
                embed_color,
            ):
                processed_ids.append(entry_id)
                if len(processed_ids) > 1000:
                    processed_ids.pop(0)
                time.sleep(delay_between_posts)

    def process_feed(self, feed_config: dict[str, Any]) -> None:
        """Process a single RSS feed."""
        feed_url = feed_config.get("url")
        webhook_url = feed_config.get("webhook")
        feed_name = feed_config.get("name", feed_url)
        webhook_name = feed_config.get("webhook_name")
        webhook_avatar = feed_config.get("webhook_avatar")
        embed_color = feed_config.get("embed_color")
        max_age_days = self.config.get("max_post_age_days", 7)

        if not feed_url or not webhook_url:
            logger.warning("Skipping feed %s: missing url or webhook", feed_name)
            return

        logger.info("Processing feed: %s", feed_name)

        try:
            feed = feedparser.parse(feed_url)

            if feed.bozo and not feed.entries:
                logger.error(
                    "Error parsing feed %s: %s",
                    feed_name,
                    feed.bozo_exception,
                )
                return

            feed_title = str(getattr(feed.feed, "title", feed_name))

            if feed_url not in self.state["feeds"]:
                self.state["feeds"][feed_url] = {"processed_ids": []}

            processed_ids = self.state["feeds"][feed_url]["processed_ids"]

            new_entries, skipped_old = self._filter_new_entries(
                feed.entries,
                processed_ids,
                max_age_days,
            )

            if skipped_old > 0:
                logger.info(
                    "Skipped %d old entries (older than %d days) in %s",
                    skipped_old,
                    max_age_days,
                    feed_name,
                )

            if new_entries:
                logger.info(
                    "Found %d new entries in %s",
                    len(new_entries),
                    feed_name,
                )
                self._process_new_entries(
                    new_entries,
                    webhook_url,
                    feed_title,
                    processed_ids,
                    webhook_name,
                    webhook_avatar,
                    embed_color,
                )
                self._save_state()
            else:
                logger.debug("No new entries in %s", feed_name)

        except Exception:
            logger.exception("Error processing feed %s", feed_name)

    def run(self) -> None:
        """Main run loop."""
        refresh_interval = self.config.get("refresh_interval", 300)
        feeds = self.config.get("feeds", [])

        if not feeds:
            logger.warning("No feeds configured")
            return

        logger.info(
            "Starting RSS to Discord with %d feeds, refresh interval: %ds",
            len(feeds),
            refresh_interval,
        )

        while True:
            try:
                for feed_config in feeds:
                    self.process_feed(feed_config)

                logger.info(
                    "Waiting %d seconds until next refresh...",
                    refresh_interval,
                )
                time.sleep(refresh_interval)

            except KeyboardInterrupt:
                logger.info("Shutting down...")
                sys.exit(0)
            except Exception:
                logger.exception("Unexpected error in main loop")
                time.sleep(60)


if __name__ == "__main__":
    app = RSSToDiscord()
    app.run()
