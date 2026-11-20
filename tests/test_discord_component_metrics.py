from dataclasses import replace

from rss2discord.discord.client import WebhookMessage
from rss2discord.discord.components import (
    MAX_METRICS_CHARACTERS,
    MAX_RENDERED_METRICS,
    METRICS_GAP,
)
from rss2discord.models import EntryData, PriceDirection, SourceMetric
from tests.discord_components_helpers import (
    get_child_contents,
    get_container_children,
    get_metrics_content,
    get_text_display_contents,
    make_message,
)


def _with_metrics(*metrics: SourceMetric) -> WebhookMessage:
    message = make_message()
    return replace(
        message,
        entry=replace(
            message.entry,
            description="",
            author="",
            timestamp=None,
            source_metrics=metrics,
        ),
    )


def test_metrics_lead_with_headline_and_stack_supporting_values_one_per_line() -> None:
    # Given
    message = _with_metrics(
        SourceMetric(label="Price", value="7.990 ден"),
        SourceMetric(label="Stock", value="3"),
        SourceMetric(label="Installments", value="12 × 5.749 ден"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - first metric at body size, each of the rest on its own subtext line
    assert metrics == (
        f"Price: **7.990 ден**\n{METRICS_GAP}\n"
        "-# Stock: 3\n-# Installments: 12 × 5.749 ден"
    )


def test_dense_alert_stacks_every_supporting_metric_on_its_own_line() -> None:
    # Given - the shape a Neksio price alert actually emits
    message = _with_metrics(
        SourceMetric(label="Price", value="11.450 ден"),
        SourceMetric(label="Previous", value="9.990 ден", prior=True),
        SourceMetric(label="Original", value="13.990 ден"),
        SourceMetric(label="Product code", value="KF560C36-32"),
        SourceMetric(label="Manufacturer", value="Kingston"),
        SourceMetric(label="Stock", value="7"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - one detail per line, so none has to be read out of a sentence
    assert metrics == (
        "Price: **11.450 ден** ~~9.990 ден~~\n"
        f"{METRICS_GAP}\n"
        "-# Original: 13.990 ден\n"
        "-# Product code: KF560C36-32\n"
        "-# Manufacturer: Kingston\n"
        "-# Stock: 7"
    )


def test_metrics_gap_is_a_blank_subtext_line() -> None:
    # Given - the gap is invisible on screen and in source, so a stray edit that
    # drops the character would go unnoticed without an explicit check

    # Then - a subtext prefix carrying nothing but a zero-width space
    assert METRICS_GAP == "-# \u200b"
    assert METRICS_GAP.strip("-# ") == "\u200b"


def test_no_gap_without_supporting_metrics() -> None:
    # Given - a headline and nothing to separate it from
    message = _with_metrics(SourceMetric(label="Price", value="7.990 ден"))

    # When
    metrics = get_metrics_content(message)

    # Then - the gap must not leave a blank line hanging off the end
    assert metrics == "Price: **7.990 ден**"
    assert METRICS_GAP not in metrics


def test_gap_is_not_charged_against_the_supporting_metrics() -> None:
    # Given - the densest shape any transport emits
    message = _with_metrics(
        SourceMetric(label="Price", value="11.450 ден"),
        SourceMetric(label="Previous", value="9.990 ден", prior=True),
        SourceMetric(label="Original", value="13.990 ден"),
        SourceMetric(label="Product code", value="KF560C36-32"),
        SourceMetric(label="Manufacturer", value="Kingston"),
        SourceMetric(label="Stock", value="7"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - reserving the gap up front must not push a real detail out
    assert METRICS_GAP in metrics
    assert "-# Stock: 7" in metrics
    assert len(metrics) <= MAX_METRICS_CHARACTERS


def test_gap_reservation_keeps_the_block_inside_its_budget() -> None:
    # Given - supporting lines sized so a fifth one fits only if the gap's own
    # characters go unreserved, which would overrun the block by a single line
    message = _with_metrics(
        SourceMetric(label="P", value="1"),
        *(SourceMetric(label="L" * 40, value="V" * 54) for _ in range(5)),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the gap is paid for up front, so the block cannot overrun
    assert METRICS_GAP in metrics
    assert len(metrics) <= MAX_METRICS_CHARACTERS


def test_metrics_render_outside_the_provenance_footer() -> None:
    # Given
    message = _with_metrics(SourceMetric(label="Price", value="7.990 ден"))

    # When
    contents = get_text_display_contents(message)

    # Then - metrics sit in the body; the footer carries provenance only
    assert contents == [
        "## [Entry](https://example.test/entry)",
        "Price: **7.990 ден**",
        "-# RSS • News",
    ]


def test_prior_metric_renders_struck_through_beside_the_headline() -> None:
    # Given
    message = _with_metrics(
        SourceMetric(label="Price", value="7.990 ден"),
        SourceMetric(label="Previous", value="9.490 ден", prior=True),
        SourceMetric(label="Original", value="10.990 ден"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the strike-through says "was", so the prior label is dropped, and
    # the prior value does not also appear in the supporting line
    assert metrics == (
        f"Price: **7.990 ден** ~~9.490 ден~~\n{METRICS_GAP}\n-# Original: 10.990 ден"
    )


def test_prior_metric_is_the_only_one_promoted_beside_the_headline() -> None:
    # Given - two metrics claim prior; only the first may take the slot
    message = _with_metrics(
        SourceMetric(label="Price", value="5"),
        SourceMetric(label="Previous", value="9", prior=True),
        SourceMetric(label="Original", value="12", prior=True),
    )

    # When
    metrics = get_metrics_content(message)

    # Then
    assert metrics == f"Price: **5** ~~9~~\n{METRICS_GAP}\n-# Original: 12"


def test_prior_flag_on_the_headline_metric_is_ignored() -> None:
    # Given - the first metric is the headline; it cannot supersede itself
    message = _with_metrics(
        SourceMetric(label="Price", value="5", prior=True),
        SourceMetric(label="Stock", value="2"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then
    assert metrics == f"Price: **5**\n{METRICS_GAP}\n-# Stock: 2"


def test_prior_metric_without_a_value_leaves_no_empty_strike_through() -> None:
    # Given - a superseded figure the source never supplied
    message = _with_metrics(
        SourceMetric(label="Price", value="7.990 ден"),
        SourceMetric(label="Previous", value="", prior=True),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - an empty strike-through would render as literal tildes
    assert metrics == "Price: **7.990 ден**"


def test_unlabelled_metric_renders_as_a_bare_value() -> None:
    # Given - no shipped source emits one, but the model permits it
    message = _with_metrics(
        SourceMetric(label="Points", value="482"),
        SourceMetric(label="", value="example.com"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - no stray colon where there is nothing to label
    assert metrics == f"Points: **482**\n{METRICS_GAP}\n-# example.com"


def test_unlabelled_headline_metric_renders_without_a_colon() -> None:
    # Given
    message = _with_metrics(SourceMetric(label="", value="482"))

    # When
    metrics = get_metrics_content(message)

    # Then
    assert metrics == "**482**"


def test_valueless_metric_renders_as_a_bare_label() -> None:
    # Given - a detail the source names but never measures
    message = _with_metrics(
        SourceMetric(label="Price", value="10"),
        SourceMetric(label="Stock", value=""),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - no colon left dangling in front of nothing
    assert metrics == f"Price: **10**\n{METRICS_GAP}\n-# Stock"


def test_metrics_beyond_the_render_cap_are_dropped() -> None:
    # Given - more metrics than the card will show
    message = _with_metrics(
        *(
            SourceMetric(label=f"L{index}", value=str(index))
            for index in range(MAX_RENDERED_METRICS + 4)
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the cap holds and the overflow leaves no empty line behind
    assert len(metrics.splitlines()) == MAX_RENDERED_METRICS + 1  # + the gap
    assert f"L{MAX_RENDERED_METRICS}" not in metrics
    assert all(metrics.splitlines())


def test_metrics_budget_skips_an_overlong_detail_and_keeps_the_rest() -> None:
    # Given - four details that nearly fill the block, then one too long for
    # what remains, then a short one that still fits
    message = _with_metrics(
        SourceMetric(label="P", value="1"),
        *(SourceMetric(label="L" * 40, value="V" * 60) for _ in range(4)),
        SourceMetric(label="X" * 48, value="Y" * 64),
        SourceMetric(label="Stock", value="7"),
    )

    # When
    metrics = get_metrics_content(message)
    lines = metrics.splitlines()

    # Then - the overlong one drops out without taking the rest with it
    assert len(metrics) <= MAX_METRICS_CHARACTERS
    assert lines[0] == "P: **1**"
    assert lines[-1] == "-# Stock: 7"
    assert not any(line.startswith("-# XXX") for line in lines)


def test_render_cap_clears_the_densest_shipped_producer() -> None:
    # Given - the most metrics any transport emits at once, from the Neptun and
    # Neksio price alerts: price, previous, original and three product details.
    densest = 6
    message = _with_metrics(
        *(
            SourceMetric(label=f"L{index}", value=str(index))
            for index in range(densest)
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - nothing a real producer emits is silently dropped
    assert densest <= MAX_RENDERED_METRICS
    for index in range(densest):
        assert f"L{index}" in metrics


def test_entry_without_metrics_renders_no_metrics_block() -> None:
    # Given
    message = _with_metrics()

    # When
    contents = get_text_display_contents(message)

    # Then
    assert contents == ["## [Entry](https://example.test/entry)", "-# RSS • News"]


def test_description_moves_below_the_section_when_metrics_take_the_thumbnail_row() -> (
    None
):
    # Given - an image, metrics and a description all at once
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="",
            timestamp=None,
            image_url="https://example.test/image.png",
            source_metrics=(SourceMetric(label="Price", value="5"),),
        ),
    )

    # When
    children = get_container_children(message)

    # Then - the section keeps title and metrics beside the thumbnail, and the
    # description drops below it to use the container's full width
    section = children[0]
    assert section["type"] == 9
    assert get_child_contents(section) == [
        "## [Entry](https://example.test/entry)",
        "Price: **5**",
    ]
    assert children[1] == {"content": "Description", "type": 10}


def test_description_stays_beside_the_thumbnail_when_there_are_no_metrics() -> None:
    # Given - an image and a description but nothing to measure
    message = make_message(
        entry=EntryData(
            title="Entry",
            link="https://example.test/entry",
            description="Description",
            author="",
            timestamp=None,
            image_url="https://example.test/image.png",
        ),
    )

    # When
    children = get_container_children(message)

    # Then - the space next to the thumbnail is not left empty
    section = children[0]
    assert get_child_contents(section) == [
        "## [Entry](https://example.test/entry)",
        "Description",
    ]


def test_metric_markdown_stays_escaped_inside_the_headline_emphasis() -> None:
    # Given - values carrying the very characters that delimit the emphasis
    message = _with_metrics(
        SourceMetric(label="Pri**ce", value="7**990"),
        SourceMetric(label="Prev", value="9~~490", prior=True),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - escaping keeps the bold and strike-through delimiters unambiguous
    assert metrics == "Pri\\*\\*ce: **7\\*\\*990** ~~9\\~\\~490~~"


def test_line_breaks_cannot_escape_the_metrics_subtext_block() -> None:
    # Given - a value carrying a newline and a heading marker
    message = _with_metrics(
        SourceMetric(label="Price", value="10"),
        SourceMetric(label="X", value="a\n# INJECTED HEADING"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - only the block's own break survives, so no heading can open
    assert metrics == f"Price: **10**\n{METRICS_GAP}\n-# X: a # INJECTED HEADING"


def test_blank_metric_emits_no_empty_text_display() -> None:
    # Given - a metric with neither label nor value
    message = _with_metrics(SourceMetric(label="", value=""))

    # When
    contents = get_text_display_contents(message)

    # Then - Discord rejects an empty Text Display, so none is produced
    assert contents == ["## [Entry](https://example.test/entry)", "-# RSS • News"]
    assert all(contents)


def test_clipped_metric_value_is_marked_as_truncated() -> None:
    # Given - a value past the per-metric ceiling
    message = _with_metrics(SourceMetric(label="Price", value="1234567890" * 10))

    # When
    metrics = get_metrics_content(message)

    # Then - a cut value must not read as a complete one
    assert metrics.endswith("…**")


def test_blank_headline_metric_leaves_no_leading_newline() -> None:
    # Given - a blank headline metric alongside a real supporting one
    message = _with_metrics(
        SourceMetric(label="", value=""),
        SourceMetric(label="A", value="B"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the block must not open on a bare newline
    assert metrics == "-# A: B"


def test_discount_badges_headline_for_discovery_cards() -> None:
    # Given - a product card with an original price and a discount
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Original", "100 MKD"),
            ),
            discount_percentage="20.0%",
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the discount is prominent in the headline and not duplicated below
    assert metrics == (
        f"Price: **80 MKD** · **20% off**\n{METRICS_GAP}\n-# Original: 100 MKD"
    )
    assert "Discount:" not in metrics


def test_discount_stays_supporting_for_price_change_cards() -> None:
    # Given - a price drop alert that also carries a discount versus original
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
                SourceMetric("Original", "100 MKD"),
            ),
            price_change_percentage="-10.0%",
            discount_percentage="20.0%",
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the headline carries the price move only, while Change and Discount
    # are labeled so the two percentages cannot be confused
    assert "Price: **80 MKD** ~~90 MKD~~" in metrics
    assert "Change: -10.0%" in metrics
    assert "Discount: 20% off" in metrics


def test_discount_badge_formats_whole_and_fractional_percentages() -> None:
    # Given - a discovery card for each discount shape
    cases = [
        ("20.0%", "20% off"),
        ("24.6%", "24.6% off"),
        ("5.00%", "5% off"),
        ("35.0%", "35% off"),
    ]

    for discount, expected_badge in cases:
        message = make_message(
            entry=EntryData(
                title="Item",
                link="https://example.test/item",
                description="",
                author="",
                timestamp=None,
                source_metrics=(SourceMetric("Price", "80 MKD"),),
                discount_percentage=discount,
            ),
        )

        # When
        metrics = get_metrics_content(message)

        # Then - whole values read cleanly, fractional values keep precision
        assert f"**{expected_badge}**" in metrics


def test_period_low_badges_headline_for_price_drop() -> None:
    # Given - a price drop that is also the period low
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
            ),
            price_change_percentage="-11.1%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the low is prominent in the headline and not duplicated below
    assert metrics == (
        "Price: **80 MKD** ~~90 MKD~~ · **30-day low**\n"
        f"{METRICS_GAP}\n"
        "-# Change: -11.1%"
    )
    assert "30-day range" not in metrics


def test_period_range_supports_price_drop_not_at_low() -> None:
    # Given - a price drop with room left in the period window
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "85 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
            ),
            price_change_percentage="-5.6%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=False,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the range is compact context that does not steal the headline
    assert "Price: **85 MKD** ~~90 MKD~~" in metrics
    assert "-# Change: -5.6%" in metrics
    assert "-# 30-day range: 80 MKD – 120 MKD" in metrics
    assert "**30-day low**" not in metrics


def test_period_low_badge_not_shown_for_price_rise() -> None:
    # Given - a price rise that happens to sit at the numeric period low
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "70 MKD", prior=True),
            ),
            price_change_percentage="+14.3%",
            price_direction=PriceDirection.INCREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the direction gate keeps the badge off increases
    assert "**30-day low**" not in metrics
    assert "30-day range" not in metrics


def test_period_range_skipped_when_values_missing_or_equal() -> None:
    # Given - a price drop that lacks a usable range
    cases: tuple[tuple[str | None, str | None], ...] = (
        (None, "120 MKD"),
        ("80 MKD", None),
        ("100 MKD", "100 MKD"),
    )

    for low, high in cases:
        message = make_message(
            entry=EntryData(
                title="Item",
                link="https://example.test/item",
                description="",
                author="",
                timestamp=None,
                source_metrics=(SourceMetric("Price", "90 MKD"),),
                price_change_percentage="-10.0%",
                price_is_period_low=False,
                price_period_low=low,
                price_period_high=high,
            ),
        )

        # When
        metrics = get_metrics_content(message)

        # Then - the range line is omitted rather than rendered incompletely
        assert "30-day range" not in metrics


def test_discount_stays_supporting_when_period_low_badges_headline() -> None:
    # Given - a price drop that is the period low and also discounted from original
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
                SourceMetric("Original", "100 MKD"),
            ),
            price_change_percentage="-11.1%",
            discount_percentage="20.0%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the low owns the headline; discount and change stay labeled below
    assert "Price: **80 MKD** ~~90 MKD~~ · **30-day low**" in metrics
    assert "-# Change: -11.1%" in metrics
    assert "-# Discount: 20% off" in metrics
    assert "30-day range" not in metrics


def test_discovery_card_ignores_history_fields() -> None:
    # Given - a discovery product card that happens to carry history context
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Original", "100 MKD"),
            ),
            discount_percentage="20.0%",
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - discovery cards render exactly as before; history fields are ignored
    assert metrics == (
        f"Price: **80 MKD** · **20% off**\n{METRICS_GAP}\n-# Original: 100 MKD"
    )
    assert "30-day low" not in metrics
    assert "30-day range" not in metrics


def test_period_low_badge_uses_price_period_days() -> None:
    # Given - a price drop with a non-default history window
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            price_change_percentage="-10.0%",
            price_direction=PriceDirection.DECREASE,
            price_period_days=7,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the badge reflects the configured window length
    assert "**7-day low**" in metrics
    assert "30-day low" not in metrics


def test_period_low_falls_back_to_percentage_sign_when_direction_missing() -> None:
    # Given - a price drop with no explicit direction field
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            price_change_percentage="-10.0%",
            price_direction=None,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the negative percentage is enough to gate the badge on
    assert "**30-day low**" in metrics


def test_period_low_suppressed_by_positive_percentage_without_direction() -> None:
    # Given - a price rise with no explicit direction field
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(SourceMetric("Price", "80 MKD"),),
            price_change_percentage="+10.0%",
            price_direction=None,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the positive percentage keeps the badge off
    assert "**30-day low**" not in metrics


def test_equal_price_restock_badges_without_redundant_previous_or_change() -> None:
    # Given - an item that came back at exactly the same price
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "80 MKD", prior=True),
                SourceMetric("Stock", "In stock"),
                SourceMetric("Merchant", "Example"),
            ),
            restocked=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the restock is prominent and nothing repeats the same figure
    assert metrics == (
        f"Price: **80 MKD** · **Back in stock**\n{METRICS_GAP}\n-# Merchant: Example"
    )
    assert "Previous" not in metrics
    assert "Change" not in metrics
    assert "+0.0%" not in metrics
    assert "Stock" not in metrics


def test_restock_with_price_drop_keeps_previous_and_change() -> None:
    # Given - an item that restocked and dropped in price at the same time
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
                SourceMetric("Stock", "7"),
            ),
            price_change_percentage="-11.1%",
            restocked=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the restock badge, previous price, and change all remain readable
    assert metrics == (
        "Price: **80 MKD** ~~90 MKD~~ · **Back in stock**\n"
        f"{METRICS_GAP}\n"
        "-# Change: -11.1%\n"
        "-# Stock: 7"
    )


def test_restock_with_period_low_shows_both_signals() -> None:
    # Given - a restock that is also the lowest price in the window
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "80 MKD"),
                SourceMetric("Previous", "90 MKD", prior=True),
            ),
            price_change_percentage="-11.1%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="80 MKD",
            price_period_high="120 MKD",
            price_is_period_low=True,
            restocked=True,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - both the restock and the period-low signals survive, neither
    # overwriting the other
    assert metrics == (
        "Price: **80 MKD** ~~90 MKD~~ · **Back in stock** · **30-day low**\n"
        f"{METRICS_GAP}\n"
        "-# Change: -11.1%"
    )


def test_period_range_skips_when_crowding_existing_metrics() -> None:
    # Given - a dense price drop where the range would be the last line
    message = make_message(
        entry=EntryData(
            title="Item",
            link="https://example.test/item",
            description="",
            author="",
            timestamp=None,
            source_metrics=(
                SourceMetric("Price", "11.450 ден"),
                SourceMetric("Previous", "12.450 ден", prior=True),
                SourceMetric("Original", "13.990 ден"),
                SourceMetric("Product code", "KF560C36-32"),
                SourceMetric("Manufacturer", "Kingston"),
                SourceMetric("Stock", "7"),
            ),
            price_change_percentage="-8.0%",
            price_direction=PriceDirection.DECREASE,
            price_period_low="11.450 ден",
            price_period_high="13.990 ден",
            price_is_period_low=False,
        ),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - the range is appended last and the existing order is preserved
    lines = metrics.splitlines()
    assert lines[0] == "Price: **11.450 ден** ~~12.450 ден~~"
    assert "-# Change: -8.0%" in metrics
    assert "-# Original: 13.990 ден" in metrics
    assert "-# Product code: KF560C36-32" in metrics
    assert "-# Manufacturer: Kingston" in metrics
    assert "-# Stock: 7" in metrics
    assert "-# 30-day range: 11.450 ден – 13.990 ден" in metrics
    # Range follows the existing supporting metrics
    assert metrics.index("30-day range") > metrics.index("Stock: 7")
