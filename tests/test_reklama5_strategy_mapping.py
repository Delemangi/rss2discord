from __future__ import annotations

import pytest

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.transports import reklama5 as reklama5_transport
from rss2discord.transports.reklama5 import Reklama5Listing
from tests.reklama5_helpers import FIXED_NOW


def _listing(
    *,
    price: str = "100 ден.",
    location: str = "Скопје",
    category: str = "Компјутери",
) -> Reklama5Listing:
    return Reklama5Listing(
        entry_id=EntryId("123"),
        url="https://reklama5.mk/AdDetails?ad=123",
        title="Listing",
        summary="Summary",
        price=price,
        location=location,
        category=category,
        activity_at=FIXED_NOW,
        image_url="https://images.example.test/123.jpg",
    )


def test_reklama5_entry_data_emits_price_then_location_metrics() -> None:
    strategy = reklama5_transport.Reklama5Strategy()
    listing = _listing()

    data = strategy.get_entry_data(listing)

    assert data == EntryData(
        title=listing.title,
        link=listing.url,
        description=listing.summary,
        author="",
        timestamp=listing.activity_at.isoformat(),
        image_url=listing.image_url,
        categories=(listing.category,),
        source_metrics=(
            SourceMetric(label="Price", value=listing.price),
            SourceMetric(label="Location", value=listing.location),
        ),
    )


@pytest.mark.parametrize(
    ("price", "location", "expected"),
    [
        ("100 ден.", "", (SourceMetric(label="Price", value="100 ден."),)),
        ("", "Скопје", (SourceMetric(label="Location", value="Скопје"),)),
        ("", "", ()),
    ],
    ids=["price", "location", "neither"],
)
def test_reklama5_entry_data_omits_absent_source_metrics(
    price: str,
    location: str,
    expected: tuple[SourceMetric, ...],
) -> None:
    strategy = reklama5_transport.Reklama5Strategy()

    data = strategy.get_entry_data(_listing(price=price, location=location))

    assert data.source_metrics == expected


def test_reklama5_entry_data_omits_absent_category() -> None:
    strategy = reklama5_transport.Reklama5Strategy()

    data = strategy.get_entry_data(_listing(category=""))

    assert data.categories == ()


def test_reklama5_strategy_exposes_delivery_lifecycle_policy() -> None:
    strategy = reklama5_transport.Reklama5Strategy()

    assert strategy.seed_existing_on_first_fetch is True
    assert strategy.require_entries_for_initialization is False
    assert strategy.max_new_entries_per_fetch is None
    assert strategy.max_delivery_history is None
    assert strategy.get_entry_id(_listing()) == "123"
