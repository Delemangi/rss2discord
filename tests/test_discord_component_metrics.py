from dataclasses import replace

from rss2discord.discord.client import WebhookMessage
from rss2discord.discord.components import (
    MAX_METRICS_CHARACTERS,
    MAX_RENDERED_METRICS,
)
from rss2discord.models import EntryData, SourceMetric
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
        "Price: **7.990 ден**\n-# Stock: 3\n-# Installments: 12 × 5.749 ден"
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
        "-# Original: 13.990 ден\n"
        "-# Product code: KF560C36-32\n"
        "-# Manufacturer: Kingston\n"
        "-# Stock: 7"
    )


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
    assert metrics == "Price: **7.990 ден** ~~9.490 ден~~\n-# Original: 10.990 ден"


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
    assert metrics == "Price: **5** ~~9~~\n-# Original: 12"


def test_prior_flag_on_the_headline_metric_is_ignored() -> None:
    # Given - the first metric is the headline; it cannot supersede itself
    message = _with_metrics(
        SourceMetric(label="Price", value="5", prior=True),
        SourceMetric(label="Stock", value="2"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then
    assert metrics == "Price: **5**\n-# Stock: 2"


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
    assert metrics == "Points: **482**\n-# example.com"


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
    assert metrics == "Price: **10**\n-# Stock"


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
    assert len(metrics.splitlines()) == MAX_RENDERED_METRICS
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


def test_description_moves_below_the_section_when_metrics_take_the_thumbnail_row(
) -> None:
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
    assert metrics == "Price: **10**\n-# X: a # INJECTED HEADING"


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
