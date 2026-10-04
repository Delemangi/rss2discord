from rss2discord.main import _format_location


def test_ordinary_interval_diagnostic_retains_safe_field_name() -> None:
    assert _format_location(("feeds", 0, "ordinary_check_interval")) == (
        "feeds.0.ordinary_check_interval"
    )
    assert _format_location(("feeds", 0, "private-setting")) == "feeds.0.<key>"
