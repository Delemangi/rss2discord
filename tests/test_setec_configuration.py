from pathlib import Path

from rss2discord.configuration import load_config


def test_load_config_parses_setec_strategy(tmp_path: Path) -> None:
    # Given
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "feeds:\n"
        "  - id: setec-new-products\n"
        "    url: https://setec.mk/e-prodazba\n"
        "    webhook: https://discord.test/webhook\n"
        "    strategy: setec\n",
    )

    # When
    config = load_config(config_path)

    # Then
    assert config.feeds[0].strategy == "setec"
