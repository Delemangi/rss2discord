from rss2discord.models import EntryData


def test_entry_data_defaults_to_no_source_metrics() -> None:
    # Given / When
    entry = EntryData(
        title="Title",
        link="https://example.test/article",
        description="Description",
        author="Author",
        timestamp=None,
    )

    # Then
    assert entry.source_metrics == ()
