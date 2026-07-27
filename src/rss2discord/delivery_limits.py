from typing import Any

from .delivery_store import DeliveryStore
from .transports import FeedFetchError, ScraperStrategy


def enforce_delivery_limits(
    feed_id: str,
    entries: list[Any],
    strategy: ScraperStrategy,
    store: DeliveryStore,
) -> None:
    new_entry_limit = strategy.max_new_entries_per_fetch
    history_limit = strategy.max_delivery_history
    if new_entry_limit is None and history_limit is None:
        return
    new_entry_ids = {
        entry_id
        for entry in entries
        if (entry_id := strategy.get_entry_id(entry)) is not None
        and not store.has_delivered(feed_id, entry_id)
    }
    if new_entry_limit is not None and len(new_entry_ids) > new_entry_limit:
        raise FeedFetchError("Feed", "NewEntryLimitExceeded")
    if (
        history_limit is not None
        and store.count_delivered(feed_id) + len(new_entry_ids) > history_limit
    ):
        raise FeedFetchError("Feed", "DeliveryHistoryLimitExceeded")
