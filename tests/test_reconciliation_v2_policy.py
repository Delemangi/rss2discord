import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from rss2discord.reconciliation import apply_plan
from rss2discord.reconciliation_models import (
    ReconciliationItem,
    ReconciliationPlan,
    ReconciliationSnapshot,
    validate_dispositions,
)
from tests.test_ddstore_price_monitor import make_feed

FIXTURE = Path(__file__).parent / "fixtures" / "reconciliation_v1"


def test_v1_artifact_bytes_and_fingerprint_remain_golden() -> None:
    artifact = (FIXTURE / "plan.json").read_bytes()
    plan = ReconciliationPlan.model_validate_json(artifact)
    assert plan.version == 1
    assert plan.model_dump_json(indent=2).encode() + b"\n" == artifact
    expected_fingerprint = (FIXTURE / "fingerprint.txt").read_text().strip()
    assert plan.reconciliation_fingerprint == expected_fingerprint
    assert plan.fingerprint() == expected_fingerprint
    validate_dispositions(plan)


def _item(
    *,
    target: ReconciliationSnapshot | None = None,
    disposition: Literal["review", "accept", "hold", "noop", "defer"] = "defer",
) -> ReconciliationItem:
    return ReconciliationItem(
        product_id="item-1",
        previous=None,
        target=target,
        context=None,
        pending=False,
        already_held=False,
        disposition=disposition,
    )


def _plan(
    item: ReconciliationItem,
    *,
    version: Literal[1, 2] = 2,
) -> ReconciliationPlan:
    return ReconciliationPlan(
        version=version,
        feed_id="ddstore",
        provider="DDStore",
        source_strategy="ddstore",
        source_url="https://ddstore.mk/shop/",
        batch_id=7,
        batch_fingerprint="1" * 64,
        batch_state_digest="2" * 64,
        snapshots_digest="3" * 64,
        holds_digest="4" * 64,
        catalog_digest="5" * 64,
        catalog_count=1,
        available_count=1,
        captured_at=1_700_000_000,
        reason="operator-reviewed test",
        items=(item,),
    )


def test_v2_defer_is_limited_to_missing_or_unavailable_and_not_existing_holds() -> None:
    validate_dispositions(_plan(_item()))
    available = _item(
        target=ReconciliationSnapshot(
            amount="1",
            formatted="1",
            currency="MKD",
        ),
    )
    with pytest.raises(ValueError, match="only valid"):
        _plan(available)
    held = _item().model_copy(update={"already_held": True})
    with pytest.raises(ValueError, match="only valid"):
        _plan(held)
    held_accept = _item(
        target=ReconciliationSnapshot(amount="1", formatted="1", currency="MKD"),
        disposition="accept",
    ).model_copy(update={"already_held": True})
    with pytest.raises(ValueError, match="existing holds"):
        validate_dispositions(_plan(held_accept))


def test_defer_is_rejected_for_v1_and_unknown_artifact_fields_or_versions() -> None:
    with pytest.raises(ValueError, match="version 2"):
        validate_dispositions(_plan(_item(), version=1))
    payload = json.loads((FIXTURE / "plan.json").read_text())
    payload["version"] = 3
    with pytest.raises(ValidationError):
        ReconciliationPlan.model_validate(payload)
    payload["version"] = 1
    payload["unexpected"] = "must not be ignored"
    with pytest.raises(ValidationError):
        ReconciliationPlan.model_validate(payload)
    payload.pop("unexpected")
    for unsupported_version in (1.0, True):
        payload["version"] = unsupported_version
        with pytest.raises(ValidationError):
            ReconciliationPlan.model_validate(payload)
    v1_missing = _plan(_item(disposition="accept"), version=1)
    with pytest.raises(
        ValueError,
        match=r"missing or unavailable products require hold$",
    ):
        validate_dispositions(v1_missing)


def test_invalid_v2_defer_is_rejected_before_catalog_fetch_or_store_access() -> None:
    plan = _plan(_item()).model_copy(
        update={
            "items": (
                _item(
                    target=ReconciliationSnapshot(
                        amount="1",
                        formatted="1",
                        currency="MKD",
                    ),
                ),
            ),
        },
    )

    class Store:
        database_path = "unused"

    class Ownership:
        def require(self, _path: object) -> None:
            return

    def forbidden_fetch(*_args: object) -> tuple[object, ...]:
        raise AssertionError("invalid artifact must be rejected before network fetch")

    with pytest.raises(ValueError, match="only valid"):
        apply_plan(
            Store(),  # type: ignore[arg-type]
            make_feed(),
            plan,
            Ownership(),  # type: ignore[arg-type]
            fingerprint=plan.fingerprint(),
            catalog_fetch=forbidden_fetch,  # type: ignore[arg-type]
        )


def test_tampered_v1_defer_is_rejected_before_catalog_fetch() -> None:
    valid = _plan(_item(disposition="hold"), version=1)
    tampered = valid.model_copy(
        update={"items": (_item(),)},
    )

    class Store:
        database_path = "unused"

    class Ownership:
        def require(self, _path: object) -> None:
            return

    def forbidden_fetch(*_args: object) -> tuple[object, ...]:
        raise AssertionError("invalid artifact must be rejected before network fetch")

    with pytest.raises(ValueError, match="version 2"):
        apply_plan(
            Store(),  # type: ignore[arg-type]
            make_feed(),
            tampered,
            Ownership(),  # type: ignore[arg-type]
            fingerprint=tampered.fingerprint(),
            catalog_fetch=forbidden_fetch,  # type: ignore[arg-type]
        )
