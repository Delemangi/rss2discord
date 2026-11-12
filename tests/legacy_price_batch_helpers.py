"""Explicitly seed pre-summary manifests for legacy approval safety regressions."""

from collections.abc import Iterable
from typing import Protocol

import pytest

from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.providers.cccenter import prices as cc_prices
from rss2discord.providers.setec import prices as setec_prices
from rss2discord.recovery_models import PriceBatch, PriceChangeRecord


class Monitor(Protocol):
    def scan(self) -> None: ...


def enable_legacy_candidates(patch: pytest.MonkeyPatch) -> None:
    def candidate(
        store: DeliveryStore,
        *,
        feed_id: str,
        provider: str,
        items: Iterable[PriceChangeRecord],
        catalog_count: int,
        available_count: int,
    ) -> PriceBatch:
        records = tuple(items)
        return store.record_price_change_candidate(
            feed_id=feed_id,
            provider=provider,
            items=records,
            fingerprint=canonical_manifest_fingerprint(
                feed_id=feed_id,
                provider=provider,
                items=records,
            ),
            catalog_count=catalog_count,
            available_count=available_count,
        )

    patch.setattr(DeliveryStore, "stage_automatic_price_summary", candidate)
    # Historical candidate capture predates bounded detail-summary confirmation.
    # Only fixture construction changes; approved individual paths remain real.
    patch.setattr(cc_prices, "MAX_UNAPPROVED_PRICE_CHANGES", 10_000)
    patch.setattr(setec_prices, "MAX_UNAPPROVED_PRICE_CHANGES", 10_000)


def scan_legacy_candidate(monitor: Monitor, store: DeliveryStore) -> None:
    del store
    with pytest.MonkeyPatch.context() as patch:
        enable_legacy_candidates(patch)
        monitor.scan()
