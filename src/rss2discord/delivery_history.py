"""Bounded price history and 30-day price-range queries for alerts."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from rss2discord.defaults import PRICE_HISTORY_WINDOW_DAYS
from rss2discord.models import PriceHistoryContext, PriceSnapshot

_HISTORY_CHUNK_SIZE = 400
_SECONDS_PER_DAY = 86_400


@dataclass(frozen=True, slots=True)
class _HistoryRow:
    amount: Decimal
    formatted: str
    currency: str
    observed_at: int


def _chunks(values: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _context_for(
    observation: PriceSnapshot,
    rows: Sequence[_HistoryRow],
    window_start: int,
) -> PriceHistoryContext | None:
    """Compute the window range for one observation from its history rows.

    Rows recorded for the observation itself are ignored inside the window so a
    product first seen moments ago gets no range; an older row is still kept as
    the in-effect price at the window start.
    """
    currency_rows = [row for row in rows if row.currency == observation.currency]
    in_window = [
        row
        for row in currency_rows
        if row.observed_at >= window_start
        and (row.amount, row.formatted) != (observation.amount, observation.formatted)
    ]
    before = [row for row in currency_rows if row.observed_at < window_start]
    baseline = max(before, key=lambda row: row.observed_at) if before else None
    candidates = [*(in_window), *([baseline] if baseline is not None else [])]
    if not candidates:
        return None
    low_amount, low_formatted = min(
        ((row.amount, row.formatted) for row in candidates),
        key=lambda candidate: candidate[0],
    )
    high_amount, high_formatted = max(
        ((row.amount, row.formatted) for row in candidates),
        key=lambda candidate: candidate[0],
    )
    # The current observation participates in the range, so a new low is
    # recognised even when the history has no matching row yet. It also owns
    # the formatted text whenever it supplies an extreme.
    if observation.amount <= low_amount:
        low_amount, low_formatted = observation.amount, observation.formatted
    if observation.amount >= high_amount:
        high_amount, high_formatted = observation.amount, observation.formatted
    return PriceHistoryContext(
        low=low_amount,
        low_formatted=low_formatted,
        high=high_amount,
        high_formatted=high_formatted,
        is_low=observation.amount <= low_amount,
    )


class PriceHistoryRepository:
    """Query append-only price history; every method is read-only."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def load_price_history_context(
        self,
        feed_id: str,
        observations: Mapping[str, PriceSnapshot],
        *,
        window_days: int = PRICE_HISTORY_WINDOW_DAYS,
        now: int | None = None,
    ) -> Mapping[str, PriceHistoryContext]:
        """Return a range per product that has history besides this observation."""
        if not observations:
            return {}
        effective_now = int(time.time()) if now is None else now
        window_start = effective_now - window_days * _SECONDS_PER_DAY
        contexts: dict[str, PriceHistoryContext] = {}
        for chunk in _chunks(tuple(observations), _HISTORY_CHUNK_SIZE):
            rows_by_product = self._rows_by_product(feed_id, chunk, effective_now)
            for product_id in chunk:
                context = _context_for(
                    observations[product_id],
                    rows_by_product.get(product_id, ()),
                    window_start,
                )
                if context is not None:
                    contexts[product_id] = context
        return contexts

    def _rows_by_product(
        self,
        feed_id: str,
        product_ids: tuple[str, ...],
        now: int,
    ) -> dict[str, tuple[_HistoryRow, ...]]:
        placeholders = ", ".join("?" for _ in product_ids)
        rows = self._connection.execute(
            "SELECT product_id, amount, formatted, currency, observed_at "  # noqa: S608
            "FROM price_history WHERE feed_id = ? "
            f"AND product_id IN ({placeholders})",
            (feed_id, *product_ids),
        )
        grouped: dict[str, list[_HistoryRow]] = {}
        for product_id, amount, formatted, currency, observed_at in rows:
            row = _parse_row(amount, formatted, currency, observed_at)
            if row is None or row.observed_at > now:
                continue
            grouped.setdefault(str(product_id), []).append(row)
        return {
            product_id: tuple(product_rows)
            for product_id, product_rows in grouped.items()
        }


def _parse_row(
    amount: object,
    formatted: object,
    currency: object,
    observed_at: object,
) -> _HistoryRow | None:
    try:
        parsed_amount = Decimal(str(amount))
        parsed_observed_at = int(str(observed_at))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return _HistoryRow(
        amount=parsed_amount,
        formatted=str(formatted),
        currency=str(currency),
        observed_at=parsed_observed_at,
    )
