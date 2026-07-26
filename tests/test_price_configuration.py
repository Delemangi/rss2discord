from pathlib import Path

import pytest
from pydantic import ValidationError

import rss2discord.main as app_main
from rss2discord.configuration import load_config
from tests.configuration_helpers import write_config


def test_load_config_disables_anhoch_price_check_interval_when_omitted(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval is None


def test_load_config_disables_anhoch_price_check_interval_when_null(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: null\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval is None


def test_load_config_parses_positive_anhoch_price_check_interval(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: 3600\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval == 3600


@pytest.mark.parametrize("interval", [0, -1])
def test_load_config_rejects_non_positive_anhoch_price_check_interval(
    tmp_path: Path,
    interval: int,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        f"    price_check_interval: {interval}\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_non_finite_anhoch_price_check_interval(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: anhoch\n"
        "    url: https://www.anhoch.test/products\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: anhoch\n"
        "    price_check_interval: .inf\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_rejects_price_check_interval_for_non_anhoch_feed(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: news\n"
        "    url: https://example.test/feed.xml\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: rss\n"
        "    price_check_interval: 3600\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_load_config_disables_setec_price_check_interval_when_omitted(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: setec\n"
        "    url: https://setec.example.test/e-prodazba\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: setec\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval is None


def test_load_config_disables_setec_price_check_interval_when_null(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: setec\n"
        "    url: https://setec.example.test/e-prodazba\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: setec\n"
        "    price_check_interval: null\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval is None


def test_load_config_parses_positive_setec_price_check_interval(
    tmp_path: Path,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: setec\n"
        "    url: https://setec.example.test/e-prodazba\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: setec\n"
        "    price_check_interval: 1800\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].price_check_interval == 1800


@pytest.mark.parametrize(
    ("strategy", "url"),
    [
        ("rss", "https://example.test/feed.xml"),
        ("xenforo", "https://forum.example.test/threads/topic.1/"),
        ("itmk_oglasnik", "https://forum.it.mk/oglasnik/"),
    ],
)
def test_load_config_rejects_price_check_interval_for_unsupported_strategy(
    tmp_path: Path,
    strategy: str,
    url: str,
) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    write_config(
        config_path,
        "  - id: unsupported\n"
        f"    url: {url}\n"
        "    webhook: https://discord.test/webhook\n"
        f"    strategy: {strategy}\n"
        "    price_check_interval: 3600\n",
    )

    # When / Then
    with pytest.raises(ValidationError):
        load_config(config_path)


def test_validation_location_allows_price_check_interval_name() -> None:
    # Given
    location = ("feeds", 0, "price_check_interval")

    # When
    formatted_location = app_main._format_location(location)

    # Then
    assert formatted_location == "feeds.0.price_check_interval"
