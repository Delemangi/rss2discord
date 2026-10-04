import json
from decimal import Decimal
from pathlib import Path

import pytest

from rss2discord import reconciliation
from rss2discord.admin import main
from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.providers.ddstore.catalog import DDStoreCatalogClient
from rss2discord.providers.ddstore.models import DDStoreProduct
from rss2discord.reconciliation_models import ReconciliationPlan
from rss2discord.recovery_models import PriceSnapshot
from rss2discord.transports.catalog_normalization import CatalogObservation
from tests.reconciliation_helpers import (
    database_state,
    reviewed,
    setup_plan,
)
from tests.test_ddstore_price_monitor import make_feed, make_product


def config_file(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps({"feeds": [make_feed().model_dump(mode="json")]}),
        encoding="utf-8",
    )
    return config


def test_operator_plan_review_apply_receipt_and_holds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        initial = setup_plan(store)
        before = database_state(store)
    config = config_file(tmp_path)
    draft_path = tmp_path / "draft.json"
    sealed_path = tmp_path / "reviewed.json"

    def catalog_fetch(
        self: DDStoreCatalogClient,
        url: str,
        **kwargs: object,
    ) -> tuple[DDStoreProduct, ...]:
        del self, kwargs
        assert url == make_feed().url
        return (make_product("1", amount=90), make_product("2", amount=Decimal("1.05")))

    monkeypatch.setattr(DDStoreCatalogClient, "fetch_catalog", catalog_fetch)
    assert (
        main(
            [
                "--database",
                str(database),
                "reconcile",
                "plan",
                "--config",
                str(config),
                "--feed-id",
                "ddstore",
                "--batch-id",
                str(initial.batch_id),
                "--batch-fingerprint",
                initial.batch_fingerprint,
                "--reason",
                "future-only recovery",
                "--writers-stopped",
                "--output",
                str(draft_path),
            ],
        )
        == 0
    )
    capsys.readouterr()
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before
    draft = ReconciliationPlan.model_validate_json(
        draft_path.read_text(encoding="utf-8"),
    )
    assert (
        main(
            [
                "reconcile",
                "review",
                "--plan",
                str(draft_path),
                "--output",
                str(sealed_path),
            ],
        )
        == 2
    )
    assert "explicit" in capsys.readouterr().out
    chosen = reviewed(draft)
    draft_path.write_text(chosen.model_dump_json(), encoding="utf-8")
    assert (
        main(
            [
                "reconcile",
                "review",
                "--plan",
                str(draft_path),
                "--output",
                str(sealed_path),
            ],
        )
        == 0
    )
    capsys.readouterr()
    assert (
        main(
            [
                "--database",
                str(database),
                "reconcile",
                "apply",
                "--config",
                str(config),
                "--feed-id",
                "ddstore",
                "--plan",
                str(sealed_path),
                "--fingerprint",
                chosen.fingerprint(),
                "--writers-stopped",
            ],
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["accepted_count"] == 1
    assert receipt["held_count"] == 1
    assert (
        main(
            ["--database", str(database), "reconcile", "holds", "--feed-id", "ddstore"],
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["count"] == 1
    assert (
        main(
            [
                "--database",
                str(database),
                "reconcile",
                "receipt",
                "--fingerprint",
                chosen.fingerprint(),
            ],
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == receipt


def test_apply_requires_manual_old_writer_shutdown_acknowledgment(
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as error:
        main(
            [
                "reconcile",
                "apply",
                "--config",
                str(tmp_path / "config.yaml"),
                "--feed-id",
                "ddstore",
                "--plan",
                str(tmp_path / "plan.json"),
                "--fingerprint",
                "0" * 64,
            ],
        )
    assert error.value.code == 2


def test_old_schema_invalid_apply_is_no_schema_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        draft = setup_plan(store)
        for table in (
            "price_product_holds",
            "price_reconciliation_items",
            "price_reconciliations",
        ):
            store._connection.execute(f"DROP TABLE {table}")
        store._connection.commit()
        before = database_state(store)
    plan = reviewed(draft)
    path = tmp_path / "reviewed.json"
    path.write_text(plan.model_dump_json(), encoding="utf-8")
    config = config_file(tmp_path)

    def drift(
        _feed: FeedConfig,
        _old: tuple[PriceSnapshot, ...],
    ) -> tuple[CatalogObservation, ...]:
        return ()

    # apply_plan's default callable is fixed at definition time; patch its
    # client's pure provider operation instead of invoking a monitor.
    def empty_catalog(
        self: DDStoreCatalogClient,
        url: str,
        **kwargs: object,
    ) -> tuple[DDStoreProduct, ...]:
        del self, url, kwargs
        return ()

    monkeypatch.setattr(DDStoreCatalogClient, "fetch_catalog", empty_catalog)
    # Provider errors and structural errors both leave legacy schemas intact.
    monkeypatch.setattr(reconciliation, "fetch_catalog", drift)
    assert (
        main(
            [
                "--database",
                str(database),
                "reconcile",
                "apply",
                "--config",
                str(config),
                "--feed-id",
                "ddstore",
                "--plan",
                str(path),
                "--fingerprint",
                plan.fingerprint(),
                "--writers-stopped",
            ],
        )
        == 2
    )
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before
