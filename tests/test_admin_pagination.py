import json
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.admin import main
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import EntryData, PriceSnapshot
from rss2discord.price_notifications import PriceNotification


def _seed_notifications(store: DeliveryStore, count: int = 3) -> None:
    notifications = tuple(
        PriceNotification(
            product_id=f"product-{index:03}",
            previous=PriceSnapshot(
                "feed",
                f"product-{index:03}",
                Decimal(100),
                "100",
                "MKD",
            ),
            current=PriceSnapshot(
                "feed",
                f"product-{index:03}",
                Decimal(90),
                "90",
                "MKD",
            ),
            entry=EntryData(
                title=f"Product {index}",
                link=f"https://shop.example.test/{index}",
                description="",
                author="",
                timestamp=None,
            ),
            source_title="Shop",
        )
        for index in range(count)
    )
    store.record_price_scan(
        feed_id="feed",
        group_id="group",
        observed_at=1_700_000_000,
        baseline_updates=tuple(item.current for item in notifications),
        notifications=notifications,
    )


def test_notifications_list_is_read_only_and_limited(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _seed_notifications(store, 3)
    before = database.read_bytes()
    assert (
        main(["--database", str(database), "notifications", "list", "--limit", "2"])
        == 0
    )
    rows = json.loads(capsys.readouterr().out)
    assert [row["title"] for row in rows] == ["Product 0", "Product 1"]
    assert (
        main(
            [
                "--database",
                str(database),
                "notifications",
                "list",
                "--status",
                "sent",
            ],
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == []
    assert database.read_bytes() == before


@pytest.mark.parametrize(
    "arguments",
    [
        ["price", "list"],
        ["baseline", "list"],
        ["reconcile", "holds"],
    ],
)
def test_removed_admin_subcommands_are_rejected(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(["--database", str(tmp_path / "state.db"), *arguments])
    assert raised.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_notifications_list_rejects_invalid_limit(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        _seed_notifications(store, 1)
    assert (
        main(["--database", str(database), "notifications", "list", "--limit", "0"])
        == 2
    )
