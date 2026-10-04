from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.configuration import AppConfig, load_config
from tests.app_helpers import make_feed


@pytest.mark.parametrize("value", [None, "missing"])
def test_ordinary_interval_omitted_or_null_inherits_global(value: object) -> None:
    feed_data = make_feed("feed").model_dump()
    feed_data.pop("ordinary_check_interval")
    if value is None:
        feed_data["ordinary_check_interval"] = value

    config = AppConfig.model_validate(
        {"refresh_interval": 900, "feeds": [feed_data]},
    )

    assert config.feeds[0].ordinary_check_interval is None
    assert config.refresh_interval == 900


def test_load_config_parses_per_feed_interval_and_keeps_price_interval_separate(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "refresh_interval: 300\n"
        "feeds:\n"
        "  - id: feed\n"
        "    url: https://example.test/feed\n"
        "    webhook: https://discord.com/api/webhooks/1/token\n"
        "    ordinary_check_interval: 3600\n"
        "    strategy: anhoch\n"
        "    price_check_interval: 7200\n",
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.feeds[0].ordinary_check_interval == 3600
    assert config.feeds[0].price_check_interval == 7200


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("-inf"), float("nan")])
def test_ordinary_interval_rejects_nonpositive_or_nonfinite_values(
    value: float,
) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "feeds": [
                    make_feed("feed").model_dump() | {"ordinary_check_interval": value},
                ],
            },
        )


@pytest.mark.parametrize("value", ["often", "Infinity", "NaN"])
def test_ordinary_interval_rejects_invalid_strings(value: str) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "feeds": [
                    make_feed("feed").model_dump() | {"ordinary_check_interval": value},
                ],
            },
        )


@pytest.mark.parametrize("value", ["often", ".inf", ".nan", "0", "-1"])
def test_yaml_rejects_invalid_ordinary_interval_values(
    tmp_path: Path,
    value: str,
) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "feeds:\n"
        "  - id: feed\n"
        "    url: https://example.test/feed\n"
        "    webhook: https://discord.com/api/webhooks/1/token\n"
        f"    ordinary_check_interval: {value}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_config(config_path)
