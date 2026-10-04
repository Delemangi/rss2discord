import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord.admin import main
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.recovery_models import PriceChangeRecord, PriceSnapshot


def _dump(database: Path) -> tuple[str, ...]:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        return tuple(connection.iterdump())
    finally:
        connection.close()


def test_price_pages_audit_all_101_items_without_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    records = tuple(
        PriceChangeRecord(
            f"product-{index:03}",
            PriceSnapshot(
                "feed",
                f"product-{index:03}",
                Decimal(index + 100),
                f"{index + 100} EUR",
                "EUR",
            ),
            PriceSnapshot(
                "feed",
                f"product-{index:03}",
                Decimal(index + 200),
                f"{index + 200} EUR",
                "EUR",
            ),
        )
        for index in reversed(range(101))
    )
    fingerprint = canonical_manifest_fingerprint(
        feed_id="feed",
        provider="test",
        items=records,
    )
    with DeliveryStore(database) as store:
        batch = store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            fingerprint=fingerprint,
            catalog_count=101,
            available_count=101,
            items=records,
        )
        store.approve_price_change_batch(
            feed_id="feed",
            fingerprint=fingerprint,
            reason="Fixture audit",
        )
        reserved = store.claim_price_delivery_attempt(batch.batch_id, "product-000")
        assert reserved is not None
        store.record_approved_price_delivery(reserved, batch.items[0].current)
    before = _dump(database)
    observed = []
    for offset in range(0, 101, 20):
        arguments = [
            "--database",
            str(database),
            "price",
            "show",
            str(batch.batch_id),
            "--offset",
            str(offset),
            "--sample-limit",
            "20",
        ]
        assert main(arguments) == 0
        page = json.loads(capsys.readouterr().out)
        assert main(arguments) == 0
        assert json.loads(capsys.readouterr().out) == page
        assert page["fingerprint"] == fingerprint
        assert page["provider"] == "test"
        assert page["offset"] == offset
        assert page["total"] == page["item_count"] == 101
        assert page["returned"] == min(20, 101 - offset)
        assert page["remaining"] == max(0, 101 - offset - 20)
        observed.extend(page["items_sample"])
    assert [item["product_id"] for item in observed] == [
        f"product-{index:03}" for index in range(101)
    ]
    for index, item in enumerate(observed):
        assert item["previous"] == {
            "amount": str(index + 100),
            "formatted": f"{index + 100} EUR",
            "currency": "EUR",
        }
        assert item["current"] == {
            "amount": str(index + 200),
            "formatted": f"{index + 200} EUR",
            "currency": "EUR",
        }
        assert item["status"] == ("delivered" if index == 0 else "pending")
        assert item["attempt_count"] == (1 if index == 0 else 0)
    assert _dump(database) == before


def test_baseline_pages_audit_all_153_ids_without_mutation(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    expected = [f"https://cccenter.mk/product/{index:03}/" for index in range(153)]
    with DeliveryStore(database) as store:
        candidate = store.record_feed_baseline_candidate(
            feed_id="cccenter",
            entry_ids=reversed(expected),
            reason="Fixture cutover",
        )
    before = _dump(database)
    observed = []
    for offset in range(0, 153, 50):
        arguments = [
            "--database",
            str(database),
            "baseline",
            "show",
            "cccenter",
            "--offset",
            str(offset),
            "--sample-limit",
            "50",
        ]
        assert main(arguments) == 0
        page = json.loads(capsys.readouterr().out)
        assert main(arguments) == 0
        assert json.loads(capsys.readouterr().out) == page
        assert page["fingerprint"] == candidate.fingerprint
        assert page["provider"] is None
        assert page["feed_id"] == "cccenter"
        assert page["offset"] == offset
        assert page["total"] == page["entry_count"] == 153
        assert page["returned"] == min(50, 153 - offset)
        assert page["remaining"] == max(0, 153 - offset - 50)
        observed.extend(page["entry_ids"])
    assert observed == expected
    assert _dump(database) == before


@pytest.mark.parametrize("kind", ["price", "baseline"])
@pytest.mark.parametrize("offset", [1, 2, 10000])
def test_show_at_or_beyond_end_is_empty(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    kind: str,
    offset: int,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        store.record_feed_baseline_candidate(
            feed_id="feed",
            entry_ids=["id"],
            reason="Fixture",
        )
        records = (
            PriceChangeRecord(
                "id",
                PriceSnapshot("feed", "id", Decimal(1), "1 EUR", "EUR"),
                PriceSnapshot("feed", "id", Decimal(2), "2 EUR", "EUR"),
            ),
        )
        batch = store.record_price_change_candidate(
            feed_id="feed",
            provider="test",
            fingerprint=canonical_manifest_fingerprint(
                feed_id="feed",
                provider="test",
                items=records,
            ),
            catalog_count=1,
            available_count=1,
            items=records,
        )
    target = str(batch.batch_id) if kind == "price" else "feed"
    assert (
        main(
            [
                "--database",
                str(database),
                kind,
                "show",
                target,
                "--offset",
                str(offset),
                "--sample-limit",
                "1000",
            ],
        )
        == 0
    )
    page = json.loads(capsys.readouterr().out)
    assert page["offset"] == offset
    assert page["total"] == 1
    assert page["returned"] == page["remaining"] == 0
    assert page["items_sample" if kind == "price" else "entry_ids"] == []


@pytest.mark.parametrize(("kind", "target"), [("price", "1"), ("baseline", "feed")])
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("--offset", "-1"),
        ("--offset", "abc"),
        ("--sample-limit", "0"),
        ("--sample-limit", "1001"),
        ("--sample-limit", "abc"),
    ],
)
def test_invalid_page_arguments_do_not_open_database(
    tmp_path: Path,
    kind: str,
    target: str,
    option: str,
    value: str,
) -> None:
    database = tmp_path / "absent" / "state.db"
    with pytest.raises(SystemExit) as error:
        main(["--database", str(database), kind, "show", target, option, value])
    assert error.value.code == 2
    assert not database.parent.exists()
