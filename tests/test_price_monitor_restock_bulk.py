"""Restock bursts should not flood a feed with individual cards."""

from rss2discord.transports.price_monitor.planning import requires_digest


def test_large_restock_burst_forces_bulk_under_a_high_inline_limit() -> None:
    # Given a 142-restock burst below the feed's 200-card inline limit
    # When / Then it is still sent as one digest
    assert requires_digest(
        record_count=142,
        restock_count=142,
        inline_limit=200,
    )


def test_normal_restock_window_keeps_individual_cards() -> None:
    # Given a modest restock window
    # When / Then the feed's inline behavior is unchanged
    assert not requires_digest(
        record_count=21,
        restock_count=21,
        inline_limit=200,
    )


def test_price_changes_still_use_the_feed_inline_limit() -> None:
    # Given a price-change burst above the feed limit and no restocks
    # When / Then the general inline limit still decides
    assert requires_digest(
        record_count=201,
        restock_count=0,
        inline_limit=200,
    )
    assert not requires_digest(
        record_count=200,
        restock_count=0,
        inline_limit=200,
    )
