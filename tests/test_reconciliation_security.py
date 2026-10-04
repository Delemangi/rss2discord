import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from rss2discord.admin import main
from rss2discord.database_ownership import DatabaseOwnership
from rss2discord.delivery_store import DeliveryStore
from rss2discord.price_safety import canonical_manifest_fingerprint
from rss2discord.reconciliation import apply_plan, create_plan
from rss2discord.reconciliation_models import (
    ReconciliationPlan,
    ReconciliationSnapshot,
    canonical_json,
    require_sanitized_context,
)
from rss2discord.recovery_models import PriceChangeRecord
from rss2discord.transports.catalog_normalization import (
    normalize_ddstore_catalog,
    normalize_hivetec_catalog,
)
from rss2discord.transports.ddstore_catalog import DDStoreCatalogClient
from rss2discord.transports.ddstore_models import DDStoreProduct
from rss2discord.transports.hivetec_catalog import HivetecCatalogClient
from rss2discord.transports.hivetec_models import HivetecProduct
from tests.reconciliation_helpers import database_state, reviewed, setup_plan
from tests.test_ddstore_price_monitor import make_feed, make_product
from tests.test_hivetec_price_monitor import feed as hivetec_feed
from tests.test_hivetec_price_monitor import product as hivetec_product

SECRET = "TEST_ONLY_SECRET"  # noqa: S105 - deliberately synthetic privacy regression marker
OPERATOR_REASON = "Operator: fixture-operator; review ticket: UNIT-R1"


@pytest.mark.parametrize("provider", ["DDStore", "Hivetec"])
@pytest.mark.parametrize(
    "field",
    ["name", "categories", "price", "regular_price", "text", "stock", "currency"],
)
@pytest.mark.parametrize("action", ["review", "apply"])
def test_nested_or_wrong_type_context_values_are_refused_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    provider: str,
    field: str,
    action: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        if provider == "DDStore":
            plan = reviewed(setup_plan(store))
            feed = make_feed()
        else:
            draft = _hivetec_draft(store, _unsafe_image_product())
            plan = draft.model_copy(
                update={
                    "items": (
                        draft.items[0].model_copy(update={"disposition": "accept"}),
                    ),
                },
            ).sealed()
            feed = hivetec_feed()
        before = database_state(store)
    context = json.loads(plan.items[0].context or "{}")
    field_name = {
        "price": "final_price" if provider == "DDStore" else "price",
        "text": "created_at" if provider == "DDStore" else "sku",
        "stock": "stock_status" if provider == "DDStore" else "is_in_stock",
    }.get(field, field)
    nested = {"images": [{"src": f"https://user:{SECRET}@private.invalid/image"}]}
    context[field_name] = (
        [nested] if field == "categories" else (1 if field == "stock" else nested)
    )
    altered = plan.model_copy(
        update={
            "items": (
                plan.items[0].model_copy(update={"context": canonical_json(context)}),
                *plan.items[1:],
            ),
        },
    )
    altered = altered.model_copy(
        update={"reconciliation_fingerprint": altered.fingerprint()},
    )
    artifact = tmp_path / "invalid.json"
    artifact.write_text(altered.model_dump_json(), encoding="utf-8")
    output = tmp_path / "sealed.json"

    def forbidden_fetch(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[HivetecProduct, ...]:
        pytest.fail("invalid context must be refused before any provider fetch")

    monkeypatch.setattr(DDStoreCatalogClient, "fetch_catalog", forbidden_fetch)
    monkeypatch.setattr(HivetecCatalogClient, "fetch_catalog", forbidden_fetch)
    if action == "review":
        args = ["reconcile", "review", "--plan", str(artifact), "--output", str(output)]
    else:
        config = tmp_path / "config.yaml"
        config.write_text(
            json.dumps({"feeds": [feed.model_dump(mode="json")]}),
            encoding="utf-8",
        )
        args = [
            "--database",
            str(database),
            "reconcile",
            "apply",
            "--config",
            str(config),
            "--feed-id",
            feed.id,
            "--plan",
            str(artifact),
            "--fingerprint",
            altered.fingerprint(),
            "--writers-stopped",
        ]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "regenerate the plan" in captured.out
    assert SECRET not in captured.out + captured.err
    assert captured.err == ""
    assert not output.exists()
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before


@pytest.mark.parametrize("provider", ["DDStore", "Hivetec"])
def test_valid_provider_projection_bytes_and_fingerprints_are_unchanged(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    provider: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        if provider == "DDStore":
            data = make_product("1", amount=90).model_dump(mode="json")
            data["categories"] = [{"name": None}, {"name": "Computers"}]
            data["price_range"]["minimum_price"]["regular_price"] = None
            product = DDStoreProduct.model_validate(data)
            draft = setup_plan(
                store,
                catalog_fetch=lambda feed, old: normalize_ddstore_catalog(
                    feed.id,
                    (product, make_product("2", amount=1)),
                    old,
                ),
            )
            plan = reviewed(draft)
            context = json.loads(plan.items[0].context or "{}")
            assert context["categories"] == [None, "Computers"]
            assert context["regular_price"] is None
        else:
            draft = _hivetec_draft(store, _unsafe_image_product())
            plan = draft.model_copy(
                update={
                    "items": (
                        draft.items[0].model_copy(update={"disposition": "accept"}),
                    ),
                },
            ).sealed()
        before_contexts = tuple(item.context for item in plan.items)
        for item in plan.items:
            assert item.context is not None
            require_sanitized_context(item.context, provider)
        before_fingerprint = plan.fingerprint()
        before_json = plan.model_dump_json(indent=2)
        artifact = tmp_path / "valid.json"
        artifact.write_text(before_json, encoding="utf-8")
        output = tmp_path / "sealed.json"
        assert (
            main(
                [
                    "reconcile",
                    "review",
                    "--plan",
                    str(artifact),
                    "--output",
                    str(output),
                ],
            )
            == 0
        )
        assert output.read_text(encoding="utf-8") == before_json + "\n"
        sealed = ReconciliationPlan.model_validate_json(output.read_bytes())
        assert sealed.fingerprint() == before_fingerprint
        assert tuple(item.context for item in sealed.items) == before_contexts
    assert SECRET not in capsys.readouterr().out


@pytest.mark.parametrize("provider", ["DDStore", "Hivetec"])
@pytest.mark.parametrize("action", ["review", "apply"])
@pytest.mark.parametrize(
    "duplicate_key",
    ["name", "categories", "source_context_digest"],
)
def test_duplicate_context_keys_are_refused_privately_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    provider: str,
    action: str,
    duplicate_key: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        if provider == "DDStore":
            plan = reviewed(setup_plan(store))
            feed = make_feed()
        else:
            draft = _hivetec_draft(store, _unsafe_image_product())
            plan = draft.model_copy(
                update={
                    "items": (
                        draft.items[0].model_copy(update={"disposition": "accept"}),
                    ),
                },
            ).sealed()
            feed = hivetec_feed()
        before = database_state(store)

    context = json.loads(plan.items[0].context or "{}")
    entries: list[str] = []
    for key, value in context.items():
        if key == duplicate_key:
            entries.append(json.dumps(key) + ":" + json.dumps({"nested": SECRET}))
        entries.append(json.dumps(key) + ":" + json.dumps(value))
    duplicated_context = "{" + ",".join(entries) + "}"
    altered = plan.model_copy(
        update={
            "items": (
                plan.items[0].model_copy(update={"context": duplicated_context}),
                *plan.items[1:],
            ),
        },
    )
    altered = altered.model_copy(
        update={"reconciliation_fingerprint": altered.fingerprint()},
    )
    artifact = tmp_path / "invalid.json"
    artifact.write_text(altered.model_dump_json(), encoding="utf-8")
    output = tmp_path / "sealed.json"

    def forbidden_fetch(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[HivetecProduct, ...]:
        pytest.fail("invalid context must be refused before any provider fetch")

    monkeypatch.setattr(DDStoreCatalogClient, "fetch_catalog", forbidden_fetch)
    monkeypatch.setattr(HivetecCatalogClient, "fetch_catalog", forbidden_fetch)
    if action == "review":
        args = ["reconcile", "review", "--plan", str(artifact), "--output", str(output)]
    else:
        config = tmp_path / "config.yaml"
        config.write_text(
            json.dumps({"feeds": [feed.model_dump(mode="json")]}),
            encoding="utf-8",
        )
        args = [
            "--database",
            str(database),
            "reconcile",
            "apply",
            "--config",
            str(config),
            "--feed-id",
            feed.id,
            "--plan",
            str(artifact),
            "--fingerprint",
            altered.fingerprint(),
            "--writers-stopped",
        ]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "regenerate the plan" in captured.out
    assert SECRET not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    assert captured.err == ""
    assert not output.exists()
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before


@pytest.mark.parametrize("provider", ["DDStore", "Hivetec"])
def test_context_type_contract_rejects_coercions_and_cross_provider_shapes(
    tmp_path: Path,
    provider: str,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        context = (
            json.loads(reviewed(setup_plan(store)).items[0].context or "{}")
            if provider == "DDStore"
            else json.loads(
                _hivetec_draft(store, _unsafe_image_product()).items[0].context or "{}",
            )
        )
    invalid: list[tuple[str, object]] = [
        ("categories", [1]),
        ("categories", [["nested"]]),
        ("name", None),
        ("currency", "EUR"),
    ]
    invalid += (
        [
            ("final_price", 90),
            ("regular_price", {}),
            ("stock_status", "unknown"),
            ("created_at", []),
        ]
        if provider == "DDStore"
        else [
            ("price", 1299),
            ("regular_price", None),
            ("categories", [None]),
            ("currency_minor_unit", "2"),
            ("currency_minor_unit", 2.0),
            ("is_in_stock", "true"),
            ("sku", []),
        ]
    )
    for field, value in invalid:
        altered = {**context, field: value}
        with pytest.raises(ValueError, match="regenerate the plan"):
            require_sanitized_context(canonical_json(altered), provider)
    other = "Hivetec" if provider == "DDStore" else "DDStore"
    with pytest.raises(ValueError, match="regenerate the plan"):
        require_sanitized_context(canonical_json(context), other)


@pytest.mark.parametrize("action", ["review", "apply"])
def test_legacy_raw_context_is_not_resealed_or_persisted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        before = database_state(store)
    raw_context = json.dumps(
        {"images": [{"src": f"https://user:{SECRET}@private.invalid/image"}]},
    )
    altered = plan.model_copy(
        update={
            "items": (
                plan.items[0].model_copy(update={"context": raw_context}),
                plan.items[1],
            ),
        },
    )
    altered = altered.model_copy(
        update={"reconciliation_fingerprint": altered.fingerprint()},
    )
    artifact = tmp_path / "legacy.json"
    artifact.write_text(altered.model_dump_json(), encoding="utf-8")
    output = tmp_path / "sealed.json"
    if action == "review":
        args = ["reconcile", "review", "--plan", str(artifact), "--output", str(output)]
    else:
        config = tmp_path / "config.yaml"
        config.write_text(
            json.dumps({"feeds": [make_feed().model_dump(mode="json")]}),
            encoding="utf-8",
        )
        args = [
            "--database",
            str(database),
            "reconcile",
            "apply",
            "--config",
            str(config),
            "--feed-id",
            "ddstore",
            "--plan",
            str(artifact),
            "--fingerprint",
            altered.fingerprint(),
            "--writers-stopped",
        ]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "regenerate the plan" in captured.out
    assert SECRET not in captured.out + captured.err
    assert captured.err == ""
    assert not output.exists()
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before


def _unsafe_image_product(password: str = SECRET) -> HivetecProduct:
    data = hivetec_product(1, "129900").model_dump(mode="json")
    data["images"] = [
        {
            "src": f"https://user:{password}@private.invalid/image",
            "thumbnail": f"https://private.invalid/image?token={password}#private",
        },
    ]
    return HivetecProduct.model_validate(data)


def _hivetec_draft(store: DeliveryStore, current: HivetecProduct) -> ReconciliationPlan:
    feed = hivetec_feed()
    previous = normalize_hivetec_catalog(feed.id, (hivetec_product(1, "149900"),), ())[
        0
    ].snapshot
    target = normalize_hivetec_catalog(feed.id, (current,), ())[0].snapshot
    assert previous is not None
    assert target is not None
    store.upsert_price_snapshot(previous)
    change = PriceChangeRecord("1", previous, target)
    fingerprint = canonical_manifest_fingerprint(
        feed_id=feed.id,
        provider="Hivetec",
        items=(change,),
    )
    batch = store.record_price_change_candidate(
        feed_id=feed.id,
        provider="Hivetec",
        fingerprint=fingerprint,
        catalog_count=1,
        available_count=1,
        items=(change,),
    )
    return create_plan(
        store,
        feed,
        batch_id=batch.batch_id,
        batch_fingerprint=fingerprint,
        reason=OPERATOR_REASON,
        catalog_fetch=lambda _feed, old: normalize_hivetec_catalog(
            feed.id,
            (current,),
            old,
        ),
    )


@pytest.mark.parametrize(
    "url",
    [
        f"https://ddstore.mk/?access_token={SECRET}#private",
        f"https://ddstore.mk/#{SECRET}",
        f"https://user:{SECRET}@ddstore.mk/",
        f"https://hivetec.mk/shop/?access_token={SECRET}",
        f"https://hivetec.mk/shop/#{SECRET}",
        f"https://user:{SECRET}@hivetec.mk/shop/",
    ],
)
@pytest.mark.parametrize("action", ["plan", "apply"])
def test_sensitive_source_urls_refused_privately_before_fetch_or_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    url: str,
    action: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        before = database_state(store)
    feed = make_feed().model_copy(
        update={
            "url": url,
            "strategy": "hivetec" if "hivetec.mk" in url else "ddstore",
        },
    )
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps({"feeds": [feed.model_dump(mode="json")]}),
        encoding="utf-8",
    )
    artifact = tmp_path / "input.json"
    artifact.write_text(plan.model_dump_json(), encoding="utf-8")
    output = tmp_path / "draft.json"

    def forbidden_fetch(
        *_args: object,
        **_kwargs: object,
    ) -> tuple[HivetecProduct, ...]:
        pytest.fail("invalid source identity must be rejected before fetching")

    monkeypatch.setattr(DDStoreCatalogClient, "fetch_catalog", forbidden_fetch)
    monkeypatch.setattr(HivetecCatalogClient, "fetch_catalog", forbidden_fetch)
    args = [
        "--database",
        str(database),
        "reconcile",
        action,
        "--config",
        str(config),
        "--feed-id",
        feed.id,
        "--writers-stopped",
    ]
    args += (
        [
            "--batch-id",
            str(plan.batch_id),
            "--batch-fingerprint",
            plan.batch_fingerprint,
            "--reason",
            OPERATOR_REASON,
            "--output",
            str(output),
        ]
        if action == "plan"
        else ["--plan", str(artifact), "--fingerprint", plan.fingerprint()]
    )
    assert main(args) == 2
    captured = capsys.readouterr()
    assert "invalid reconciliation source URL" in captured.out
    assert SECRET not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err
    assert not output.exists()
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before


def test_artifact_source_validation_hides_input_and_rejects_without_stripping(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        plan = reviewed(setup_plan(store))
    data = plan.model_dump(mode="json")
    data["source_url"] = f"https://ddstore.mk/?access_token={SECRET}#private"
    with pytest.raises(
        ValidationError,
        match="invalid reconciliation source URL",
    ) as error:
        ReconciliationPlan.model_validate(data)
    assert SECRET not in str(error.value)


def test_unsafe_image_context_is_not_retained_in_artifacts_or_immutable_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "state.db"
    product = _unsafe_image_product()
    assert product.image_url is None
    with DeliveryStore(database) as store:
        draft = _hivetec_draft(store, product)
    config = tmp_path / "config.yaml"
    config.write_text(
        json.dumps({"feeds": [hivetec_feed().model_dump(mode="json")]}),
        encoding="utf-8",
    )

    def fresh(
        self: HivetecCatalogClient,
        url: str,
        **_kwargs: object,
    ) -> tuple[HivetecProduct, ...]:
        del self
        assert url == hivetec_feed().url
        return (product,)

    monkeypatch.setattr(HivetecCatalogClient, "fetch_catalog", fresh)
    draft_path = tmp_path / "draft.json"
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
                "hivetec",
                "--batch-id",
                str(draft.batch_id),
                "--batch-fingerprint",
                draft.batch_fingerprint,
                "--reason",
                OPERATOR_REASON,
                "--writers-stopped",
                "--output",
                str(draft_path),
            ],
        )
        == 0
    )
    generated = ReconciliationPlan.model_validate_json(draft_path.read_bytes())
    context = json.loads(generated.items[0].context or "{}")
    assert context["name"] == product.name
    assert context["price"] == "1299"
    assert len(context["source_context_digest"]) == 64
    edited = generated.model_copy(
        update={
            "items": (generated.items[0].model_copy(update={"disposition": "accept"}),),
        },
    )
    edited_path = tmp_path / "edited.json"
    edited_path.write_text(edited.model_dump_json(), encoding="utf-8")
    sealed_path = tmp_path / "sealed.json"
    assert (
        main(
            [
                "reconcile",
                "review",
                "--plan",
                str(edited_path),
                "--output",
                str(sealed_path),
            ],
        )
        == 0
    )
    sealed = ReconciliationPlan.model_validate_json(sealed_path.read_bytes())
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
                "hivetec",
                "--plan",
                str(sealed_path),
                "--fingerprint",
                sealed.fingerprint(),
                "--writers-stopped",
            ],
        )
        == 0
    )
    for path in (draft_path, edited_path, sealed_path):
        retained = path.read_text(encoding="utf-8")
        assert SECRET not in retained
        assert "private.invalid" not in retained
    with DeliveryStore(database, read_only=True) as store:
        assert SECRET not in "\n".join(database_state(store))
        assert "private.invalid" not in "\n".join(database_state(store))
        assert store._connection.execute(
            "SELECT COUNT(*) FROM price_reconciliations",
        ).fetchone() == (1,)
        assert store._connection.execute(
            "SELECT COUNT(*) FROM price_reconciliation_items",
        ).fetchone() == (1,)
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err


def test_discarded_unsafe_image_drift_still_rejects_apply_with_zero_writes(
    tmp_path: Path,
) -> None:
    database = tmp_path / "state.db"
    with DatabaseOwnership(database) as ownership, DeliveryStore(database) as store:
        product = _unsafe_image_product()
        draft = _hivetec_draft(store, product)
        plan = draft.model_copy(
            update={
                "items": (draft.items[0].model_copy(update={"disposition": "accept"}),),
            },
        ).sealed()
        changed = _unsafe_image_product(SECRET + "_CHANGED")
        old_context = json.loads(
            normalize_hivetec_catalog("hivetec", (product,), ())[0].context,
        )
        new_context = json.loads(
            normalize_hivetec_catalog("hivetec", (changed,), ())[0].context,
        )
        assert old_context.pop("source_context_digest") != new_context.pop(
            "source_context_digest",
        )
        assert old_context == new_context
        before = database_state(store)
        with pytest.raises(ValueError, match="drift") as error:
            apply_plan(
                store,
                hivetec_feed(),
                plan,
                ownership,
                fingerprint=plan.fingerprint(),
                catalog_fetch=lambda _feed, old: normalize_hivetec_catalog(
                    "hivetec",
                    (changed,),
                    old,
                ),
            )
        assert SECRET not in str(error.value)
        assert database_state(store) == before


@pytest.mark.parametrize("action", ["plan", "apply"])
def test_malformed_yaml_is_private_controlled_refusal_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        before = database_state(store)
    config = tmp_path / "malformed.yaml"
    config.write_text(
        f'feeds:\n  - webhook: "https://discord.invalid/{SECRET}\n',
        encoding="utf-8",
    )
    artifact = tmp_path / "reviewed.json"
    artifact.write_text(plan.model_dump_json(), encoding="utf-8")
    output = tmp_path / "draft.json"
    args = [
        "--database",
        str(database),
        "reconcile",
        action,
        "--config",
        str(config),
        "--feed-id",
        "ddstore",
        "--writers-stopped",
    ]
    args += (
        [
            "--batch-id",
            str(plan.batch_id),
            "--batch-fingerprint",
            plan.batch_fingerprint,
            "--reason",
            OPERATOR_REASON,
            "--output",
            str(output),
        ]
        if action == "plan"
        else ["--plan", str(artifact), "--fingerprint", plan.fingerprint()]
    )
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out.strip() == "invalid YAML reconciliation configuration"
    assert captured.err == ""
    assert not output.exists()
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before


@pytest.mark.parametrize(
    "amount",
    [
        SECRET,
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "0.0000000000001",
        "1234567890123",
        "1e99999999999999999999",
    ],
)
@pytest.mark.parametrize("action", ["review", "apply"])
def test_invalid_amounts_are_private_controlled_refusals_without_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    amount: str,
    action: str,
) -> None:
    database = tmp_path / "state.db"
    with DeliveryStore(database) as store:
        plan = reviewed(setup_plan(store))
        before = database_state(store)
    data = plan.model_dump(mode="json")
    data["items"][0]["target"]["amount"] = amount
    artifact = tmp_path / "invalid.json"
    artifact.write_text(json.dumps(data), encoding="utf-8")
    output = tmp_path / "sealed.json"
    if action == "review":
        args = ["reconcile", "review", "--plan", str(artifact), "--output", str(output)]
    else:
        config = tmp_path / "config.yaml"
        config.write_text(
            json.dumps({"feeds": [make_feed().model_dump(mode="json")]}),
            encoding="utf-8",
        )
        args = [
            "--database",
            str(database),
            "reconcile",
            "apply",
            "--config",
            str(config),
            "--feed-id",
            "ddstore",
            "--plan",
            str(artifact),
            "--fingerprint",
            plan.fingerprint(),
            "--writers-stopped",
        ]
    assert main(args) == 2
    captured = capsys.readouterr()
    assert captured.out.strip() == "invalid reconciliation artifact or configuration"
    assert captured.err == ""
    assert not output.exists()
    with pytest.raises(ValidationError, match="invalid reconciliation amount") as error:
        ReconciliationSnapshot(amount=amount, formatted="current", currency="MKD")
    assert SECRET not in str(error.value)
    with DeliveryStore(database, read_only=True) as store:
        assert database_state(store) == before
