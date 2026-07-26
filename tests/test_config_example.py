from pathlib import Path

from rss2discord.configuration import load_config


def test_checked_in_config_example_enables_hourly_anhoch_price_monitoring() -> None:
    # Given
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    # When
    config = load_config(example_path)
    anhoch_feed = next(
        feed for feed in config.feeds if feed.id == "anhoch-new-products"
    )

    # Then
    assert anhoch_feed.price_check_interval == 3600


def test_checked_in_config_example_documents_neksio_price_monitoring() -> None:
    # Given
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    # When
    config = load_config(example_path)
    neksio_feed = next(feed for feed in config.feeds if feed.id == "neksio-products")

    # Then
    assert neksio_feed.url == "https://g.store.neksio.mk/"
    assert neksio_feed.strategy == "neksio"
    assert neksio_feed.price_check_interval == 3600
