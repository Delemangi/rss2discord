from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from typing import Final, Protocol

from rss2discord.discord.message import WebhookMessage
from rss2discord.models import PriceSnapshot
from rss2discord.providers.catalog_price_monitor import CatalogPriceMonitor
from rss2discord.providers.reklama5.parser import Reklama5Listing
from rss2discord.providers.reklama5.scope import REKLAMA5_LABEL
from rss2discord.providers.reklama5.strategy import Reklama5Strategy
from rss2discord.retries import FetchRetryPolicy
from rss2discord.transports.price_monitor import build_price_change_message

MAX_REKLAMA5_RETAINED_SNAPSHOTS: Final = 10_000
_MAX_PRICE_DIGITS: Final = 10
_PRICE_AMOUNT_PATTERN: Final = r"(?P<amount>(?:\d{1,3}(?:[ .,\u00a0]\d{3})+|\d+))"
_MKD_PRICE_PATTERN: Final = re.compile(
    rf"{_PRICE_AMOUNT_PATTERN}\s*(?:ден\.?|МКД)",
    re.IGNORECASE,
)
_EUR_PRICE_PATTERN: Final = re.compile(
    rf"{_PRICE_AMOUNT_PATTERN}\s*(?:€|EUR)",
    re.IGNORECASE,
)


class Reklama5Catalog(Protocol):
    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[Reklama5Listing, ...]: ...


class Reklama5PriceMonitor(CatalogPriceMonitor[Reklama5Listing]):
    provider = "reklama5"
    label = REKLAMA5_LABEL
    allow_currency_changes = True

    @property
    def snapshot_limit(self) -> int:
        return MAX_REKLAMA5_RETAINED_SNAPSHOTS

    def _snapshot(self, listing: Reklama5Listing) -> PriceSnapshot | None:
        for pattern, currency in (
            (_MKD_PRICE_PATTERN, "MKD"),
            (_EUR_PRICE_PATTERN, "EUR"),
        ):
            match = pattern.fullmatch(listing.price.strip())
            if match is None:
                continue
            digits = re.sub(r"[ .,\u00a0]", "", match.group("amount"))
            if len(digits) > _MAX_PRICE_DIGITS:
                return None
            amount = Decimal(digits)
            if amount <= 0:
                return None
            return PriceSnapshot(
                self._feed.id,
                str(listing.entry_id),
                amount,
                listing.price,
                currency,
            )
        return None

    def _message_for(
        self,
        listing: Reklama5Listing,
        previous: PriceSnapshot,
        current: PriceSnapshot,
    ) -> WebhookMessage:
        return build_price_change_message(
            feed=self._feed,
            base_entry=Reklama5Strategy().get_entry_data(listing),
            previous=previous,
            current=current,
            source_title=self._feed.name or REKLAMA5_LABEL,
        )
