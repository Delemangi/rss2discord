from pathlib import Path

import pytest

from rss2discord.admin import main
from rss2discord.delivery_store import DeliveryStore


def test_admin_health_and_price_list_are_local_and_bounded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database):
        pass

    assert main(["--database", str(database), "health"]) == 0
    assert capsys.readouterr().out.strip() == "[]"
    assert main(["--database", str(database), "price", "list"]) == 0
    assert capsys.readouterr().out.strip() == "[]"
