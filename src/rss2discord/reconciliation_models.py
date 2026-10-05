"""Versioned canonical operator artifacts, separate from delivery manifests."""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, DecimalException
from typing import ClassVar, Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.recovery_models import PriceSnapshot

MAX_RECONCILIATION_ITEMS = 70_000
MAX_PLAN_BYTES = 100_000_000
MAX_PLAN_AGE_SECONDS = 86_400
MAX_OPERATION_SECONDS = 300.0


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


class ArtifactModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
    )


class ReconciliationSnapshot(ArtifactModel):
    amount: str
    formatted: str = Field(min_length=1, max_length=256)
    currency: Literal["MKD"]

    @field_validator("amount")
    @classmethod
    def require_canonical_amount(cls, value: str) -> str:
        try:
            canonical = canonicalize_price_amount(Decimal(value))
        except (DecimalException, ValueError):
            raise ValueError("invalid reconciliation amount") from None
        if canonical != value:
            raise ValueError("reconciliation amount must be canonical")
        return value

    @classmethod
    def from_snapshot(cls, snapshot: PriceSnapshot) -> ReconciliationSnapshot:
        if snapshot.currency != "MKD":
            raise ValueError("reconciliation supports only MKD snapshots")
        return cls(
            amount=canonicalize_price_amount(snapshot.amount),
            formatted=snapshot.formatted,
            currency="MKD",
        )  # type: ignore[arg-type]

    def snapshot(self, feed_id: str, product_id: str) -> PriceSnapshot:
        return PriceSnapshot(
            feed_id,
            product_id,
            Decimal(self.amount),
            self.formatted,
            self.currency,
        )


class ReconciliationItem(ArtifactModel):
    product_id: str = Field(min_length=1, max_length=256)
    previous: ReconciliationSnapshot | None
    target: ReconciliationSnapshot | None
    context: str | None = Field(max_length=100_000)
    pending: bool
    already_held: bool
    disposition: Literal["review", "accept", "hold", "noop", "defer"]
    reason: str = Field(default="", max_length=2048)


class ReconciliationPlan(ArtifactModel):
    version: Literal[1, 2] = 1
    feed_id: str = Field(min_length=1, max_length=256)
    provider: Literal["DDStore", "Hivetec"]
    source_strategy: Literal["ddstore", "hivetec"]
    source_url: str = Field(min_length=1, max_length=2048)
    batch_id: int = Field(gt=0)
    batch_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    batch_state_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    snapshots_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    holds_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    catalog_count: int = Field(gt=0, le=20_000)
    available_count: int = Field(ge=0, le=20_000)
    captured_at: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=2048)
    items: tuple[ReconciliationItem, ...] = Field(max_length=MAX_RECONCILIATION_ITEMS)
    reconciliation_fingerprint: str = ""

    @model_validator(mode="after")
    def require_safe_source(self) -> Self:
        require_safe_source_url(self.source_url, self.source_strategy)
        for item in self.items:
            if item.disposition != "defer":
                continue
            if self.version != 2:
                raise ValueError("defer requires reconciliation version 2")
            if item.target is not None or item.already_held:
                raise ValueError(
                    "defer is only valid for missing or unavailable products",
                )
        return self

    @field_validator("version", mode="before")
    @classmethod
    def require_exact_version(cls, value: object) -> object:
        if type(value) is not int or value not in (1, 2):
            raise ValueError("unsupported reconciliation version")
        return value

    def fingerprint(self) -> str:
        return digest(
            self.model_dump(mode="json", exclude={"reconciliation_fingerprint"}),
        )

    def sealed(self) -> ReconciliationPlan:
        validate_dispositions(self)
        return self.model_copy(
            update={"reconciliation_fingerprint": self.fingerprint()},
        )


def validate_dispositions(plan: ReconciliationPlan) -> None:
    require_safe_source_url(plan.source_url, plan.source_strategy)
    if (
        plan.provider
        != {"ddstore": "DDStore", "hivetec": "Hivetec"}[plan.source_strategy]
    ):
        raise ValueError("reconciliation source identity mismatch")
    if not plan.reason.strip():
        raise ValueError("operator reason is required")
    ids = tuple(item.product_id for item in plan.items)
    if ids != tuple(sorted(set(ids))):
        raise ValueError("reconciliation IDs must be unique and sorted")
    for item in plan.items:
        if item.context is not None:
            require_sanitized_context(item.context, plan.provider)
        if item.disposition == "review":
            raise ValueError(
                "every review item needs an explicit accept, hold, or noop decision",
            )
        if item.already_held and item.disposition != "hold":
            raise ValueError("existing holds cannot be released by reconciliation")
        if item.disposition == "defer":
            if plan.version != 2:
                raise ValueError("defer requires reconciliation version 2")
            if item.target is not None or item.already_held:
                raise ValueError(
                    "defer is only valid for missing or unavailable products",
                )
        if item.target is None and item.disposition not in {"hold", "defer"}:
            message = (
                "missing or unavailable products require hold"
                if plan.version == 1
                else "missing or unavailable products require hold or defer"
            )
            raise ValueError(message)
        if item.disposition == "noop" and item.previous != item.target:
            raise ValueError("noop requires an unchanged snapshot")
        if item.disposition == "accept" and item.target is not None:
            if Decimal(item.target.amount) <= 0:
                raise ValueError("accepted price must be available")
            if (
                item.previous is not None
                and item.previous.currency != item.target.currency
            ):
                raise ValueError("accepted currency must match the baseline")


def require_safe_source_url(url: str, strategy: str) -> None:
    """Reject sensitive components, never silently strip source identity."""
    try:
        parsed = urlsplit(url)
        port = 443 if parsed.port is None else parsed.port
        safe = (
            strategy in {"ddstore", "hivetec"}
            and parsed.scheme == "https"
            and parsed.hostname
            == {"ddstore": "ddstore.mk", "hivetec": "hivetec.mk"}.get(strategy)
            and port == 443
            and parsed.username is None
            and parsed.password is None
            and "?" not in url
            and "#" not in url
            and all(
                character.isprintable() and not character.isspace() for character in url
            )
            # Match the Hivetec client's exact /shop/ source contract without
            # importing the transport package into the persistence models.
            and (strategy != "hivetec" or url == "https://hivetec.mk/shop/")
        )
    except ValueError:
        safe = False
    if not safe:
        raise ValueError("invalid reconciliation source URL")


def require_sanitized_context(context: str, provider: str) -> None:
    """Refuse legacy raw-model contexts when sealing or applying artifacts."""
    try:
        value = json.loads(context, object_pairs_hook=_reject_duplicate_json_keys)
    except ValueError:
        raise ValueError(
            "invalid reconciliation context; regenerate the plan",
        ) from None
    ddstore_fields = {
        "name",
        "categories",
        "created_at",
        "stock_status",
        "final_price",
        "currency",
        "regular_price",
        "source_context_digest",
    }
    hivetec_fields = {
        "name",
        "categories",
        "sku",
        "is_in_stock",
        "price",
        "regular_price",
        "currency",
        "currency_minor_unit",
        "source_context_digest",
    }
    fields = {"DDStore": ddstore_fields, "Hivetec": hivetec_fields}.get(provider)
    if not isinstance(value, dict) or fields is None or set(value) != fields:
        raise ValueError("invalid reconciliation context; regenerate the plan")
    fingerprint = value["source_context_digest"]
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(character not in "0123456789abcdef" for character in fingerprint)
    ):
        raise ValueError("invalid reconciliation context; regenerate the plan")
    categories = value["categories"]
    common_valid = (
        isinstance(value["name"], str)
        and value["currency"] == "MKD"
        and isinstance(categories, list)
        and all(
            isinstance(category, str) or (provider == "DDStore" and category is None)
            for category in categories
        )
    )
    if provider == "DDStore":
        projection_valid = (
            isinstance(value["created_at"], str)
            and value["stock_status"] in ("IN_STOCK", "OUT_OF_STOCK")
            and _is_context_price(value["final_price"])
            and _is_context_price(value["regular_price"], nullable=True)
        )
    else:
        projection_valid = (
            isinstance(value["sku"], str)
            and isinstance(value["is_in_stock"], bool)
            and type(value["currency_minor_unit"]) is int
            and value["currency_minor_unit"] == 2
            and _is_context_price(value["price"])
            and _is_context_price(value["regular_price"])
        )
    if not common_valid or not projection_valid:
        raise ValueError("invalid reconciliation context; regenerate the plan")


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def _is_context_price(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str):
        return False
    try:
        ReconciliationSnapshot.require_canonical_amount(value)
    except ValueError:
        return False
    return True
