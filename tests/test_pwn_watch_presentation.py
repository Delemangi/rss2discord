from dataclasses import replace

import pytest

from rss2discord.discord.components import MAX_TEXT_DISPLAY_CHARACTERS
from rss2discord.discord.pwn_watch import is_pwn_watch_feed, present_pwn_watch
from rss2discord.models import EntryData
from tests.discord_components_helpers import (
    get_container,
    get_text_display_contents,
    make_message,
)


@pytest.mark.parametrize(
    ("prefix", "severity"),
    [
        ("critical", "critical"),
        ("high", "high"),
        ("medium", "medium"),
        ("low", "low"),
        ("unknown", "unknown"),
    ],
)
def test_pwn_watch_severity_prefix_is_displayed_separately(
    prefix: str,
    severity: str,
) -> None:
    result = present_pwn_watch(f"[{prefix}] Headline", "Faithful summary")
    assert result.title == "Headline"
    assert result.severity == severity


def test_pwn_watch_unknown_prefix_and_backslash_title_are_preserved() -> None:
    result = present_pwn_watch(r"[urgent] Keep \this", "Body")
    assert result.title == r"[urgent] Keep \this"
    assert result.severity is None


@pytest.mark.parametrize(
    "url",
    [
        "https://pwn.watch/all/feed.xml",
        "https://pwn.watch.evil/all/feed.xml",
        "https://evilpwn.watch/all/feed.xml",
        "https://pwn.watch:443/all/feed.xml",
        "https://user@pwn.watch/all/feed.xml",
        "https://pwn.watch/all/feed.xml?x=1",
        "https://pwn.watch/all/feed.xml#fragment",
        "https://pwn.watch\n/all/feed.xml",
        " https://pwn.watch/all/feed.xml",
        "https://pwn.watch/other/feed.xml",
    ],
)
def test_pwn_watch_feed_match_is_exact_and_strategy_scoped(url: str) -> None:
    assert is_pwn_watch_feed("rss", url) is (url == "https://pwn.watch/all/feed.xml")
    assert not is_pwn_watch_feed("xenforo", url)


def test_pwn_watch_card_has_source_severity_headline_spacing_and_quiet_footer() -> None:
    title = "[high] Swell freezes swETH and rswETH after suspected admin key compromise inflates their NAV"
    entry = EntryData(
        title=title,
        link="https://pwn.watch/i/swell-sweth-rsweth-admin-key-compromise-oct-2026",
        description="Swell summary.\n\nLatest updates:\n2026-10-23: Update. (source)2026-10-22: Earlier update. (source)2026-10-21: Source run-on. (source)2026-10-20: Earlier.",
        author="",
        timestamp="2026-10-24T09:58:00+00:00",
        categories=("high", "breach"),
    )
    message = make_message(
        url="https://pwn.watch/all/feed.xml",
        entry=entry,
        source_title="pwn.watch: all incidents updates",
    )
    contents = get_text_display_contents(message)
    assert contents[0] == "**HIGH SEVERITY**"
    assert contents[1] == f"## [{title.removeprefix('[high] ')}]({entry.link})"
    assert "(source)\n2026-10-20" in contents[2]
    assert contents[-1].startswith("-# pwn.watch • breach • <t:")
    assert "all incidents updates" not in contents[-1]
    assert get_container(message)["accent_color"] == 0xE67E22


def test_pwn_watch_exact_duplicate_is_removed_without_semantic_dedup_or_mutation() -> (
    None
):
    original = EntryData(
        title="[medium] Event",
        link="https://pwn.watch/i/event",
        description="Event\n\nEvent happened again.",
        author="",
        timestamp=None,
    )
    message = make_message(url="https://pwn.watch/all/feed.xml", entry=original)
    body = get_text_display_contents(message)[2]
    assert body == "Event happened again."
    assert original.title == "[medium] Event"
    assert original.description == "Event\n\nEvent happened again."


@pytest.mark.parametrize(
    ("title", "description", "expected"),
    [
        ("[high] Acme", "Acme confirms a breach.", "Acme confirms a breach."),
        (
            "[high] Acme",
            "Acme confirms a breach.\nMore facts.",
            "Acme confirms a breach.\nMore facts.",
        ),
        (
            "[high] Acme",
            "  Acme  \n\nAcme confirms a breach.",
            "Acme confirms a breach.",
        ),
        (
            "[high] Acme",
            "[high] Acme\n\nAcme confirms a breach.",
            "Acme confirms a breach.",
        ),
        ("[high] Acme", "", ""),
        ("[high] ", "   factual description", "   factual description"),
    ],
)
def test_pwn_watch_dedup_requires_complete_nonempty_first_line(
    title: str,
    description: str,
    expected: str,
) -> None:
    assert present_pwn_watch(title, description).description == expected


def test_pwn_watch_footer_retains_severity_categories_unless_badged_duplicate() -> None:
    without_title_severity = EntryData(
        title="Unprefixed incident",
        link="https://pwn.watch/i/unprefixed",
        description="Summary",
        author="",
        timestamp=None,
        categories=("high", "breach"),
    )
    message = make_message(
        url="https://pwn.watch/all/feed.xml",
        entry=without_title_severity,
    )
    assert get_text_display_contents(message)[-1] == "-# pwn.watch • high • breach"

    conflicting = replace(without_title_severity, title="[critical] Incident")
    conflicting_message = make_message(
        url="https://pwn.watch/all/feed.xml",
        entry=conflicting,
    )
    assert get_text_display_contents(conflicting_message)[-1] == (
        "-# pwn.watch • high • breach"
    )


def test_pwn_watch_budget_includes_badge_and_safe_old_renderer_handles_hostile_title() -> (
    None
):
    entry = EntryData(
        title="[critical] " + "[\n@everyone\\" * 500,
        link="https://pwn.watch/i/hostile",
        description="D" * 5000,
        author="A" * 5000,
        timestamp="invalid",
        categories=("critical",),
    )
    message = make_message(url="https://pwn.watch/all/feed.xml", entry=entry)
    contents = get_text_display_contents(message)
    assert sum(map(len, contents)) <= MAX_TEXT_DISPLAY_CHARACTERS
    assert len(contents[1]) <= 1024
    assert "\\[" in contents[1]
    assert get_container(message)["components"]
    assert "critical •" not in contents[-1]


def test_other_rss_card_remains_generic_for_pwn_looking_title() -> None:
    message = make_message(
        entry=replace(
            EntryData(
                title="[high] Generic",
                link="https://example.test",
                description="Description",
                author="",
                timestamp=None,
            ),
        ),
    )
    contents = get_text_display_contents(message)
    assert contents[0] == "## [[high] Generic](https://example.test)"
    assert contents[1] == "Description"
