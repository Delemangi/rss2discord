"""IT.mk Oglasnik index scraping strategy."""

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports.base import FeedFetchError, ScraperStrategy
from rss2discord.transports.itmk_oglasnik_http import (
    ITMK_OGLASNIK_LABEL,
    fetch_itmk_oglasnik_page,
)

LISTING_PATH_PATTERN: Final = re.compile(
    r"/oglasnik/[^/?#]*\.(?P<listing_id>[0-9]+)/?$",
)
PLACEHOLDER_IMAGE_NAME: Final = "no-product-image.png"


@dataclass(frozen=True, slots=True)
class ITMkOglasnikListing:
    """Normalized listing parsed from one marketplace index card."""

    entry_id: EntryId
    url: str
    title: str
    summary: str
    seller: str
    created_at: str | None
    image_url: str | None
    categories: tuple[str, ...]
    source_metrics: tuple[SourceMetric, ...]


class ITMkOglasnikStrategy(ScraperStrategy):
    """Scrape server-rendered IT.mk Oglasnik index and category pages."""

    def fetch_entries(self, url: str) -> tuple[list[ITMkOglasnikListing], str]:
        """Fetch and normalize marketplace listing cards."""
        html, final_url = fetch_itmk_oglasnik_page(url)
        soup = BeautifulSoup(html, "html.parser")
        listings = [
            listing
            for card in soup.select(".structItem")
            if (listing := self._parse_card(card, final_url)) is not None
        ]
        if not listings:
            raise FeedFetchError(ITMK_OGLASNIK_LABEL, "EmptyResponse")

        source_title = self._text(soup.select_one("h1.p-title-value"))
        if not source_title:
            source_title = self._text(soup.select_one("h1")) or ITMK_OGLASNIK_LABEL

        dated_listings: list[tuple[datetime, ITMkOglasnikListing]] = []
        undated_listings: list[ITMkOglasnikListing] = []
        for listing in listings:
            if listing.created_at is None:
                undated_listings.append(listing)
                continue
            dated_listings.append((datetime.fromisoformat(listing.created_at), listing))
        dated_listings.sort(key=lambda item: item[0])
        return [
            listing for _, listing in dated_listings
        ] + undated_listings, source_title

    def get_entry_id(self, entry: ITMkOglasnikListing) -> EntryId:
        """Return the numeric marketplace listing ID."""
        return entry.entry_id

    def get_entry_data(self, entry: ITMkOglasnikListing) -> EntryData:
        """Map one normalized marketplace listing to Discord entry data."""
        return EntryData(
            title=entry.title,
            link=entry.url,
            description=entry.summary,
            author=entry.seller,
            timestamp=entry.created_at,
            image_url=entry.image_url,
            categories=entry.categories,
            source_metrics=entry.source_metrics,
        )

    def _parse_card(
        self,
        card: Tag,
        base_url: str,
    ) -> ITMkOglasnikListing | None:
        listing_link: Tag | None = None
        listing_url: str | None = None
        listing_id: EntryId | None = None
        for candidate in card.select(".structItem-title a[href]"):
            href = self._attribute(candidate, "href")
            if href is None:
                continue
            candidate_url = urljoin(base_url, href)
            match = LISTING_PATH_PATTERN.fullmatch(urlsplit(candidate_url).path)
            if match is None:
                continue
            listing_link = candidate
            listing_url = candidate_url
            listing_id = EntryId(match.group("listing_id"))
            break

        title = self._text(listing_link)
        summary = self._text(card.select_one(".structItem-listingDescription"))
        if listing_url is None or listing_id is None or not title or not summary:
            return None

        seller = self._attribute(card, "data-author") or self._text(
            card.select_one(".username"),
        )
        created_at = self._timestamp(
            card.select_one(".structItem-startDate time[datetime]"),
        )
        category = self._text(
            card.select_one("a[href*='/oglasnik/categories/']"),
        )
        sale_status = self._text(card.select_one(".structItem-title .ribbon"))
        locked_node = card.select_one(".structItem-status--locked")
        locked_status = self._attribute(locked_node, "title") or self._text(
            locked_node,
        )
        categories = tuple(
            value for value in (category, sale_status, locked_status) if value
        )

        metadata: dict[str, str] = {}
        for field in card.select(".structItem-cell--listingMeta dl"):
            label = self._text(field.select_one("dt")).rstrip(":")
            value_node = field.select_one("dd")
            value = (
                self._timestamp(value_node.select_one("time[datetime]"))
                if value_node
                else None
            )
            value = value or self._text(value_node)
            if label and value:
                metadata[label] = value

        price = self._text(card.select_one(".structItem-statuses .ribbon"))
        condition = next(
            (
                value
                for label, value in metadata.items()
                if label.startswith("Состојба")
            ),
            "",
        )
        source_metrics = tuple(
            SourceMetric(label=label, value=value)
            for label, value in (
                ("Price", price),
                ("Condition", condition),
                ("Type", metadata.get("Тип", "")),
                ("Expires", metadata.get("Истекува", "")),
                ("Views", metadata.get("Прегледи", "")),
            )
            if value
        )

        image_node = card.select_one(".structItem-cell--icon img")
        image_path = self._attribute(image_node, "src") or self._attribute(
            image_node,
            "data-src",
        )
        image_url = urljoin(base_url, image_path) if image_path else None
        if image_url and urlsplit(image_url).path.endswith(PLACEHOLDER_IMAGE_NAME):
            image_url = None

        return ITMkOglasnikListing(
            entry_id=listing_id,
            url=listing_url,
            title=title,
            summary=self._truncate(summary),
            seller=seller,
            created_at=created_at,
            image_url=image_url,
            categories=categories,
            source_metrics=source_metrics,
        )

    @staticmethod
    def _text(node: Tag | None) -> str:
        if node is None:
            return ""
        return " ".join(node.get_text(" ", strip=True).split())

    @staticmethod
    def _attribute(node: Tag | None, name: str) -> str | None:
        if node is None:
            return None
        value = node.get(name)
        if not isinstance(value, str):
            return None
        normalized = value.strip()
        return normalized or None

    @classmethod
    def _timestamp(cls, node: Tag | None) -> str | None:
        value = cls._attribute(node, "datetime")
        return cls._parse_timestamp(value) if value is not None else None
