from dataclasses import replace

from rss2discord.discord.client import WebhookMessage
from rss2discord.discord.components import MAX_RENDERED_METRICS
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


def test_metrics_lead_with_headline_and_trail_supporting_values_as_subtext() -> None:
    # Given
    message = _with_metrics(
        SourceMetric(label="Price", value="7.990 ден"),
        SourceMetric(label="Stock", value="3"),
        SourceMetric(label="Installments", value="12 × 5.749 ден"),
    )

    # When
    metrics = get_metrics_content(message)

    # Then - first metric at body size, the rest demoted to one subtext line
    assert metrics == (
        "Price: **7.990 ден**\n-# Stock: 3 • Installments: 12 × 5.749 ден"
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


def test_unlabelled_metric_renders_as_a_bare_value() -> None:
    # Given - the Hacker News article host carries no label
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

    # Then - the cap holds and the overflow leaves no trailing separator
    assert metrics.count("•") == MAX_RENDERED_METRICS - 2
    assert f"L{MAX_RENDERED_METRICS}" not in metrics
    assert not metrics.endswith("•")


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
