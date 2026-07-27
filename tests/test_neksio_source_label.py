from rss2discord.configuration import FeedConfig
from rss2discord.discord.source_labels import source_label


def test_source_label_returns_neksio_for_neksio_feed() -> None:
    # Given
    feed = FeedConfig(
        id="neksio-products",
        url="https://g.store.neksio.mk/",
        webhook="https://discord.test/neksio",
        strategy="neksio",
    )

    # When
    label = source_label(feed)

    # Then
    assert label == "Neksio"
