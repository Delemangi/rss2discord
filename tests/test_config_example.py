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


def test_checked_in_config_example_enables_hourly_setec_price_monitoring() -> None:
    # Given
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    # When
    config = load_config(example_path)
    setec_feed = next(feed for feed in config.feeds if feed.id == "setec-new-products")

    # Then
    assert setec_feed.price_check_interval == 3600


def test_checked_in_config_example_documents_hivetec_monitoring() -> None:
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    config = load_config(example_path)
    hivetec_feed = next(feed for feed in config.feeds if feed.id == "hivetec-products")

    assert hivetec_feed.url == "https://hivetec.mk/shop/"
    assert hivetec_feed.strategy == "hivetec"
    assert hivetec_feed.price_check_interval == 3600


def test_checked_in_config_example_documents_reklama5_computer_parts() -> None:
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    config = load_config(example_path)
    feed = next(feed for feed in config.feeds if feed.id == "reklama5-computer-parts")

    assert feed.strategy == "reklama5"
    assert feed.url == (
        "https://reklama5.mk/Search?cat=584&sell=1&buy=0&trade=0"
        "&includeOld=1&includeNew=1"
    )
    assert feed.price_check_interval == 3600


def test_checked_in_config_example_documents_neptun_category_monitoring() -> None:
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    config = load_config(example_path)
    neptun_feed = next(feed for feed in config.feeds if feed.id == "neptun-computers")

    assert neptun_feed.url == "https://www.neptun.mk/KOMPJUTERI.nspx"
    assert neptun_feed.strategy == "neptun"
    assert neptun_feed.price_check_interval == 3600


def test_checked_in_config_example_documents_gjirafa50_monitoring() -> None:
    example_path = Path(__file__).parent.parent / "config" / "config.example.yaml"

    config = load_config(example_path)
    feed = next(feed for feed in config.feeds if feed.id == "gjirafa50-products")

    assert feed.url == "https://gjirafa50.mk/"
    assert feed.strategy == "gjirafa50"
    assert feed.price_check_interval == 21_600
