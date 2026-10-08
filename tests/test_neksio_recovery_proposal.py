from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordWebhookClient
from rss2discord.providers.neksio import client as neksio_client
from rss2discord.providers.neksio.catalog import NeksioCatalogClient
from rss2discord.providers.neksio.client import NEKSIO_ORIGIN
from rss2discord.providers.neksio.prices import NeksioPriceMonitor
from rss2discord.providers.neksio.reconciliation import (
    CapturedCatalogPage,
    NeksioCatalogCapture,
    build_recovery_proposal,
)
from rss2discord.providers.neksio.strategy import NeksioStrategy
from rss2discord.reconciliation_models import (
    ReconciliationPlan,
    RecoveryHold,
    RecoveryPreflightState,
)
from rss2discord.recovery_models import BaselineCandidateSummary, PriceSnapshot
from tests.neksio_helpers import (
    ProductCardPayload,
    catalog_request,
    homepage_payload,
    page_payload,
    product_card,
)

FEED_ID = "neksio-products"
STARTED = datetime(2026, 10, 9, 9, 0, tzinfo=UTC)


def _seed_store(path: Path, *, delivered: tuple[str, ...] = ()) -> None:
    with DeliveryStore(path) as store:
        store.seed_feed(FEED_ID, delivered)
        if delivered:
            store.upsert_price_snapshots(
                (
                    PriceSnapshot(
                        FEED_ID,
                        product_id,
                        Decimal(1200),
                        "1.200 ден.",
                        "MKD",
                    )
                    for product_id in delivered
                ),
            )


def _state(path: Path) -> RecoveryPreflightState:
    with DeliveryStore(path, read_only=True) as store:
        return store.read_recovery_preflight_state(FEED_ID)


def _page(
    category_id: int,
    cards: tuple[ProductCardPayload, ...],
    *,
    page_number: int = 1,
    page_count: int = 1,
    total: int | None = None,
    request_body: bytes | None = None,
    response_body: bytes | None = None,
) -> CapturedCatalogPage:
    request = (
        request_body
        or json.dumps(
            catalog_request(category_id, page_number),
        ).encode()
    )
    response = response_body or page_payload(
        category_id,
        page_number,
        page_count,
        len(cards) if total is None else total,
        cards,
    )
    return CapturedCatalogPage(request, response)


def _capture(
    pages: tuple[CapturedCatalogPage, ...],
    *,
    categories: tuple[int, ...] = (1,),
    started_at: datetime = STARTED,
    finished_at: datetime | None = None,
    homepage_body: bytes | None = None,
    source_url: str = NEKSIO_ORIGIN,
) -> NeksioCatalogCapture:
    return NeksioCatalogCapture(
        feed_id=FEED_ID,
        source_url=source_url,
        started_at=started_at,
        finished_at=finished_at or started_at + timedelta(seconds=1),
        homepage_body=homepage_body or homepage_payload(categories),
        pages=pages,
    )


def _single_capture(
    cards: tuple[ProductCardPayload, ...],
    *,
    category_id: int = 1,
    total: int | None = None,
    request_body: bytes | None = None,
    response_body: bytes | None = None,
    started_at: datetime = STARTED,
    finished_at: datetime | None = None,
    homepage_body: bytes | None = None,
    source_url: str = NEKSIO_ORIGIN,
) -> NeksioCatalogCapture:
    return _capture(
        (
            _page(
                category_id,
                cards,
                total=total,
                request_body=request_body,
                response_body=response_body,
            ),
        ),
        categories=(category_id,),
        started_at=started_at,
        finished_at=finished_at,
        homepage_body=homepage_body,
        source_url=source_url,
    )


def test_readonly_preflight_refuses_writes_and_preserves_database_bytes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database, delivered=("12",))
    before = database.read_bytes()
    with DeliveryStore(database, read_only=True) as store:
        writes = {
            sqlite3.SQLITE_INSERT,
            sqlite3.SQLITE_UPDATE,
            sqlite3.SQLITE_DELETE,
            sqlite3.SQLITE_CREATE_INDEX,
            sqlite3.SQLITE_CREATE_TABLE,
            sqlite3.SQLITE_CREATE_TRIGGER,
            sqlite3.SQLITE_DROP_INDEX,
            sqlite3.SQLITE_DROP_TABLE,
            sqlite3.SQLITE_DROP_TRIGGER,
            sqlite3.SQLITE_ALTER_TABLE,
        }

        def authorizer(action: int, *_args: object) -> int:
            return sqlite3.SQLITE_DENY if action in writes else sqlite3.SQLITE_OK

        store._connection.set_authorizer(authorizer)
        state = store.read_recovery_preflight_state(FEED_ID)
        assert state.delivered_ids == ("12",)
        assert state.snapshots[0].product_id == "12"
        assert not store._connection.in_transaction

    assert database.read_bytes() == before


def test_readonly_preflight_refuses_writable_store_even_without_initialization(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    with (
        DeliveryStore(database, initialize=False) as store,
        pytest.raises(ValueError, match="read-only"),
    ):
        store.read_recovery_preflight_state(FEED_ID)


def test_readonly_preflight_does_not_create_missing_database(tmp_path: Path) -> None:
    database = tmp_path / "missing" / "state.db"

    with pytest.raises(sqlite3.OperationalError):
        DeliveryStore(database, read_only=True)

    assert not database.exists()


def test_readonly_preflight_rejects_unsupported_schema_without_repair(
    tmp_path: Path,
) -> None:
    database = tmp_path / "partial.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE delivered_entries(feed_id TEXT)")
    before = database.read_bytes()

    with (
        DeliveryStore(database, read_only=True) as store,
        pytest.raises(ValueError, match="unsupported recovery preflight schema"),
    ):
        store.read_recovery_preflight_state(FEED_ID)

    assert database.read_bytes() == before


def test_snapshot_overflow_is_rejected_before_unbounded_snapshot_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        store.seed_feed(FEED_ID, ())
        store.upsert_price_snapshots(
            (
                PriceSnapshot(FEED_ID, "1", Decimal(1), "1 ден.", "MKD"),
                PriceSnapshot(FEED_ID, "2", Decimal(2), "2 ден.", "MKD"),
            ),
        )
    monkeypatch.setattr("rss2discord.delivery_store.MAX_RECOVERY_PRICE_IDS", 1)
    with DeliveryStore(database, read_only=True) as store:
        monkeypatch.setattr(
            store,
            "load_price_snapshots",
            lambda *_args, **_kwargs: pytest.fail(
                "unbounded reader called before count",
            ),
        )
        with pytest.raises(ValueError, match="snapshots exceed limit"):
            store.read_recovery_preflight_state(FEED_ID)


def test_preflight_rejects_nested_transaction_without_rolling_it_back(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    with DeliveryStore(database, read_only=True) as store:
        store._connection.execute("BEGIN")
        with pytest.raises(ValueError, match="active transaction"):
            store.read_recovery_preflight_state(FEED_ID)
        assert store._connection.in_transaction
        store._connection.rollback()


def test_preflight_wal_read_uses_one_snapshot_across_second_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database, delivered=("1",))
    with sqlite3.connect(database) as writer:
        writer.execute("PRAGMA journal_mode = WAL")
    with DeliveryStore(database, read_only=True) as store:
        original_count = store._count_feed_rows
        inserted = False

        def count_then_write(table: str, feed_id: str) -> int:
            nonlocal inserted
            count = original_count(table, feed_id)
            if table == "delivered_entries" and not inserted:
                with sqlite3.connect(database) as writer:
                    writer.execute(
                        "INSERT INTO delivered_entries(feed_id, entry_id) VALUES (?, ?)",
                        (FEED_ID, "2"),
                    )
                inserted = True
            return count

        monkeypatch.setattr(store, "_count_feed_rows", count_then_write)
        state = store.read_recovery_preflight_state(FEED_ID)
        assert state.delivered_ids == ("1",)
        assert not store._connection.in_transaction

    with sqlite3.connect(database) as verify:
        assert tuple(
            row[0]
            for row in verify.execute(
                "SELECT entry_id FROM delivered_entries WHERE feed_id = ? ORDER BY entry_id",
                (FEED_ID,),
            )
        ) == ("1", "2")


def test_preflight_projects_baselines_cursor_batches_claims_holds_and_releases(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        store.seed_feed(FEED_ID, ("3",))
        candidate = store.record_feed_baseline_candidate(
            feed_id=FEED_ID,
            entry_ids=("4",),
            reason="complete capture",
        )
        store.approve_feed_baseline_candidate(
            feed_id=FEED_ID,
            fingerprint=candidate.fingerprint,
            reason="approved baseline",
        )
        connection = store._connection
        connection.execute(
            "INSERT INTO price_normal_delivery_cursors(feed_id,last_product_id) "
            "VALUES (?, ?)",
            (FEED_ID, "3"),
        )
        batch_ids: dict[str, int] = {}
        for status in ("candidate", "approved", "completed", "revoked"):
            cursor = connection.execute(
                "INSERT INTO price_change_batches "
                "(feed_id,provider,fingerprint,catalog_count,available_count,status) "
                "VALUES (?, 'neksio', ?, 1, 1, ?)",
                (FEED_ID, f"fingerprint-{status}", status),
            )
            if cursor.lastrowid is None:
                raise AssertionError("price batch insert did not return a row ID")
            batch_ids[status] = cursor.lastrowid
        connection.execute(
            "INSERT INTO price_change_batches "
            "(feed_id,provider,fingerprint,catalog_count,available_count,status) "
            "VALUES (?, 'foreign', 'foreign-fingerprint', 1, 1, 'completed')",
            (FEED_ID,),
        )
        connection.execute(
            "INSERT INTO price_change_batch_items "
            "(batch_id,product_id,previous_amount,previous_formatted,previous_currency,"
            "current_amount,current_formatted,current_currency,ordinal,claim_open) "
            "VALUES (?, '9', '1', 'old', 'MKD', '2', 'new', 'MKD', 0, 1)",
            (batch_ids["revoked"],),
        )
        connection.execute(
            "INSERT INTO price_reconciliations "
            "(reconciliation_fingerprint,feed_id,batch_id,reason,plan_json,"
            "accepted_count,held_count,noop_count) VALUES (?, ?, ?, 'reason', '{}', 0, 1, 0)",
            ("a" * 64, FEED_ID, batch_ids["candidate"]),
        )
        connection.execute(
            "INSERT INTO price_product_holds "
            "(feed_id,product_id,reconciliation_fingerprint,reason,kind) "
            "VALUES (?, '9', ?, 'review', 'availability')",
            (FEED_ID, "a" * 64),
        )
        connection.execute(
            "INSERT INTO price_hold_releases "
            "(feed_id,product_id,originating_reconciliation_fingerprint,adopted_amount,"
            "adopted_formatted,adopted_currency,context,source,provider) "
            "VALUES (?, '8', ?, '1', 'one', 'MKD', '{}', 'manual', 'neksio')",
            (FEED_ID, "a" * 64),
        )
        connection.commit()

    state = _state(database)

    assert state.initialized_at is not None
    assert state.baseline_candidate is not None
    assert state.baseline_candidate.status == "approved"
    assert state.baseline_state is not None
    assert state.baseline_state[1]
    assert state.baselined_ids == ("4",)
    assert state.normal_cursor is not None
    assert state.normal_cursor[0] == "3"
    assert state.batch_counts == (1, 1, 0, 2, 1)
    assert state.foreign_provider_batch_count == 1
    assert state.open_claim_count == 1
    assert state.reconciliation_count == 1
    assert state.hold_release_count == 1
    assert len(state.holds) == 1
    assert state.holds[0].product_id == "9"
    assert state.holds[0].kind == "availability"


def test_proposal_keeps_handled_and_pending_candidate_evidence_distinct(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database, delivered=("1",))
    without_candidate = _state(database)
    with DeliveryStore(database) as store:
        store.record_feed_baseline_candidate(
            feed_id=FEED_ID,
            entry_ids=("2",),
            reason="pending",
        )
    state = _state(database)
    capture = _single_capture((product_card(1),))

    proposal = build_recovery_proposal(state, capture)
    items = {item.product_id: item for item in proposal.items}

    assert items["1"].price_status == "unchanged"
    assert items["1"].discovery_status == "already_handled"
    assert set(items) == {"1"}
    assert state.baseline_candidate is not None
    assert state.state_digest != without_candidate.state_digest
    assert "pending_baseline_candidate" in proposal.limitations
    assert proposal.limitations[:2] == (
        "offline_evidence_not_authenticated",
        "not_an_application_plan",
    )
    assert proposal.applicable is False


def test_maximum_pending_candidate_evidence_does_not_expand_proposal_inventory(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    state = replace(
        _state(database),
        baseline_candidate=BaselineCandidateSummary(
            FEED_ID,
            "a" * 64,
            tuple(str(product_id) for product_id in range(1, 70_001)),
            status="candidate",
            reason="pending evidence",
            created_at=10,
        ),
    )

    proposal = build_recovery_proposal(
        state,
        _single_capture((product_card(80_000),)),
    )

    assert len(proposal.items) == 1
    assert proposal.items[0].product_id == "80000"
    assert "pending_baseline_candidate" in proposal.limitations


def test_pending_candidate_evidence_retains_independent_size_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    state = replace(
        _state(database),
        baseline_candidate=BaselineCandidateSummary(
            FEED_ID,
            "a" * 64,
            ("2", "3"),
            status="candidate",
            reason="pending evidence",
            created_at=10,
        ),
    )
    monkeypatch.setattr(
        "rss2discord.providers.neksio.reconciliation.MAX_RECOVERY_REVIEW_ITEMS",
        1,
    )

    with pytest.raises(ValueError, match="baseline candidate exceeds limit"):
        build_recovery_proposal(
            state,
            _single_capture((product_card(80_000),)),
        )


def test_proposal_reviews_changed_price_and_rejects_noncanonical_cursor(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database, delivered=("1",))
    state = _state(database)
    changed = product_card(1)
    changed["priceWTax"] = 1500
    changed["priceWTax_f"] = "1.500 ден."
    item = build_recovery_proposal(
        state,
        _single_capture((changed,)),
    ).items[0]
    assert item.price_status == "review"

    with pytest.raises(ValueError, match="canonical positive decimal IDs"):
        build_recovery_proposal(
            replace(state, normal_cursor=("01", 10)),
            _single_capture((product_card(1),)),
        )


def test_price_retention_union_is_rechecked_after_catalog_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        store.seed_feed(FEED_ID, ())
        store.upsert_price_snapshot(
            PriceSnapshot(FEED_ID, "2", Decimal(2), "2 ден.", "MKD"),
        )
    state = _state(database)
    monkeypatch.setattr(
        "rss2discord.providers.neksio.reconciliation.MAX_RECOVERY_PRICE_IDS",
        1,
    )
    with pytest.raises(ValueError, match="price-retention union"):
        build_recovery_proposal(state, _single_capture((product_card(1),)))


def test_full_delivery_history_does_not_consume_price_retention_budget(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        store.seed_feed(FEED_ID, (str(product_id) for product_id in range(1, 50_001)))
    state = _state(database)
    proposal = build_recovery_proposal(
        state,
        _single_capture((product_card(60_000),)),
    )

    assert len(proposal.items) == 50_001
    new_product = next(item for item in proposal.items if item.product_id == "60000")
    assert new_product.price_status == "review"


def test_existing_hold_has_precedence_and_does_not_mark_discovery_handled(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database, delivered=("1",))
    state = replace(
        _state(database),
        holds=(RecoveryHold("1", "a" * 64, "review", 3, "availability"),),
    )
    proposal = build_recovery_proposal(
        state,
        _single_capture((product_card(1),)),
    )
    item = proposal.items[0]

    assert item.price_status == "existing_hold"
    assert item.discovery_status == "already_handled"
    assert "availability_holds" in proposal.limitations


def test_pending_price_snapshot_missing_from_catalog_is_reviewed(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        store.seed_feed(FEED_ID, ())
        store.upsert_price_snapshot(
            PriceSnapshot(FEED_ID, "99", Decimal(1200), "1.200 ден.", "MKD"),
        )
    proposal = build_recovery_proposal(
        _state(database),
        _single_capture((product_card(1),)),
    )
    missing = next(item for item in proposal.items if item.product_id == "99")

    assert missing.price_status == "review"
    assert missing.target is None
    assert missing.discovery_status == "not_observed"


def test_minus_one_stock_is_preserved_as_raw_evidence_and_minus_two_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    card = product_card(1)
    card["quantity"] = -1
    item = build_recovery_proposal(
        _state(database),
        _single_capture((card,)),
    ).items[0]
    context = json.loads(item.context or "{}")
    assert context["stock_quantity"] == 0
    assert context["raw_stock_quantity"] == -1

    card["quantity"] = -2
    with pytest.raises(ValueError, match="below the source contract"):
        build_recovery_proposal(_state(database), _single_capture((card,)))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("productId", True),
        ("productId", "1"),
        ("productId", 1.5),
        ("quantity", True),
        ("quantity", "7"),
        ("quantity", 7.5),
    ],
)
def test_capture_rejects_coerced_json_ids_and_quantities(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    card = product_card(1)
    card[field] = value  # type: ignore[literal-required]
    with pytest.raises(ValueError, match="JSON integers"):
        build_recovery_proposal(_state(database), _single_capture((card,)))


def test_capture_rejects_duplicate_keys_and_nonfinite_json_numbers(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    card = json.dumps(product_card(1)).encode()
    duplicate_page = b'{"categoryId":1,"categoryId":1}'
    with pytest.raises(ValueError, match="ambiguous"):
        build_recovery_proposal(
            _state(database),
            _single_capture(
                (product_card(1),),
                response_body=duplicate_page,
            ),
        )

    nonfinite_card = card.replace(b'"priceWTax": 1200', b'"priceWTax": NaN')
    with pytest.raises(ValueError, match="ambiguous"):
        build_recovery_proposal(
            _state(database),
            _single_capture(
                (product_card(1),),
                response_body=page_payload(1, 1, 1, 1, (json.loads(nonfinite_card),)),
            ),
        )


@pytest.mark.parametrize(
    ("capture_change", "message"),
    [
        ("missing_page", "incomplete"),
        ("extra_page", "extra"),
        ("wrong_request", "request does not match"),
        ("category_total", "invalid captured Neksio catalog page"),
        ("cross_category_quantity", "conflicting cross-category"),
    ],
)
def test_capture_requires_complete_consistent_inventory(
    tmp_path: Path,
    capture_change: str,
    message: str,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    card = product_card(1)
    if capture_change == "missing_page":
        capture = _capture((), categories=(1,))
    elif capture_change == "extra_page":
        capture = _capture((_page(1, (card,)), _page(1, (card,))), categories=(1,))
    elif capture_change == "wrong_request":
        capture = _capture(
            (
                _page(
                    2,
                    (card,),
                    request_body=json.dumps(catalog_request(2, 1)).encode(),
                ),
            ),
            categories=(1,),
        )
    elif capture_change == "category_total":
        capture = _single_capture((card,), total=2)
    else:
        conflicting = product_card(1)
        conflicting["quantity"] = 8
        capture = _capture(
            (_page(1, (card,)), _page(2, (conflicting,))),
            categories=(1, 2),
        )
    with pytest.raises(ValueError, match=message):
        build_recovery_proposal(_state(database), capture)


def test_duplicate_card_cannot_satisfy_declared_unique_category_total(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    pages = (
        _page(
            1,
            tuple(product_card(product_id) for product_id in range(1, 101)),
            page_number=1,
            page_count=2,
            total=101,
        ),
        _page(1, (product_card(1),), page_number=2, page_count=2, total=101),
    )
    with pytest.raises(ValueError, match="duplicate product"):
        build_recovery_proposal(_state(database), _capture(pages))


def test_empty_category_requires_its_page_and_combined_catalog_nonempty(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    empty = _page(1, (), page_count=0, total=0)
    nonempty = _page(2, (product_card(2),))
    capture = _capture((empty, nonempty), categories=(1, 2))

    assert build_recovery_proposal(_state(database), capture).catalog_count == 1
    with pytest.raises(ValueError, match="catalog is empty"):
        build_recovery_proposal(
            _state(database),
            _capture((empty,), categories=(1,)),
        )


def test_capture_enforces_source_time_and_response_budgets(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    normal = _single_capture((product_card(1),))
    with pytest.raises(ValueError, match="exact Neksio origin"):
        build_recovery_proposal(
            _state(database),
            replace(normal, source_url="https://g.store.neksio.mk/path"),
        )
    with pytest.raises(ValueError, match="aware UTC"):
        build_recovery_proposal(
            _state(database),
            replace(normal, started_at=STARTED.replace(tzinfo=None)),
        )
    with pytest.raises(ValueError, match="duration"):
        build_recovery_proposal(
            _state(database),
            replace(normal, finished_at=STARTED + timedelta(seconds=301)),
        )
    with pytest.raises(ValueError, match="homepage exceeds"):
        build_recovery_proposal(
            _state(database),
            replace(normal, homepage_body=b"x" * (1_048_576 + 1)),
        )


def test_capture_and_catalog_digests_exclude_times_but_include_source_changes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    first_capture = _single_capture((product_card(1),))
    later_capture = replace(
        first_capture,
        started_at=STARTED + timedelta(minutes=1),
        finished_at=STARTED + timedelta(minutes=2),
    )
    first = build_recovery_proposal(_state(database), first_capture)
    later = build_recovery_proposal(_state(database), later_capture)

    assert first.capture_digest == later.capture_digest
    assert first.catalog_digest == later.catalog_digest
    assert first.proposal_digest != later.proposal_digest

    changed_card = product_card(1)
    changed_card["quantity"] = 8
    changed = build_recovery_proposal(
        _state(database),
        _single_capture((changed_card,)),
    )
    assert changed.capture_digest != first.capture_digest
    assert changed.catalog_digest != first.catalog_digest

    image_changed_card = product_card(1)
    image_changed_card["imagePath"] = "/images/only-this-changed.png"
    image_changed = build_recovery_proposal(
        _state(database),
        _single_capture((image_changed_card,)),
    )
    first_context = json.loads(first.items[0].context or "{}")
    changed_context = json.loads(image_changed.items[0].context or "{}")
    assert image_changed.capture_digest != first.capture_digest
    assert image_changed.catalog_digest != first.catalog_digest
    assert (
        changed_context["source_context_digest"]
        != first_context["source_context_digest"]
    )
    assert "/images/1.png" not in (first.items[0].context or "")
    assert "/images/only-this-changed.png" not in (image_changed.items[0].context or "")


def test_proposal_cannot_be_parsed_as_an_applicable_reconciliation_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "state.db"
    _seed_store(database)
    state = _state(database)

    def no_provider_execution(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("offline proposal invoked provider execution")

    monkeypatch.setattr(NeksioCatalogClient, "fetch_catalog", no_provider_execution)
    monkeypatch.setattr(neksio_client, "fetch_homepage", no_provider_execution)
    monkeypatch.setattr(neksio_client, "fetch_page_content", no_provider_execution)
    monkeypatch.setattr(NeksioStrategy, "fetch_entries", no_provider_execution)
    monkeypatch.setattr(NeksioPriceMonitor, "scan", no_provider_execution)
    monkeypatch.setattr(DiscordWebhookClient, "send", no_provider_execution)
    proposal = build_recovery_proposal(state, _single_capture((product_card(1),)))
    assert not proposal.applicable
    with pytest.raises(ValidationError):
        ReconciliationPlan.model_validate_json(proposal.model_dump_json())
