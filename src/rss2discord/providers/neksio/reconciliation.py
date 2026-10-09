"""Offline-only validation and proposal generation for captured Neksio evidence."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal, cast

from pydantic import ValidationError

from rss2discord.fetch_errors import FeedFetchError
from rss2discord.price_amount import canonicalize_price_amount
from rss2discord.providers.neksio.catalog import (
    MAX_NEKSIO_CATEGORIES,
    MAX_NEKSIO_CATEGORY_ID,
    MAX_NEKSIO_PAGES_PER_CATEGORY,
    MAX_NEKSIO_PRODUCTS,
    _catalog_request,
    _category_ids,
    _validate_page,
)
from rss2discord.providers.neksio.client import (
    MAX_NEKSIO_RESPONSE_BYTES,
    MAX_NEKSIO_SCAN_BYTES,
    MAX_NEKSIO_SCAN_RESPONSES,
    MAX_NEKSIO_SCAN_SECONDS,
    NEKSIO_ORIGIN,
)
from rss2discord.providers.neksio.models import (
    MAX_SQLITE_SIGNED_INTEGER,
    NeksioCatalogPage,
    NeksioProduct,
)
from rss2discord.reconciliation_models import (
    MAX_RECOVERY_PRICE_IDS,
    MAX_RECOVERY_REVIEW_ITEMS,
    NeksioRecoveryProposal,
    NeksioRecoveryProposalItem,
    ReconciliationSnapshot,
    RecoveryPreflightState,
    canonical_json,
    digest,
)
from rss2discord.recovery_models import PriceSnapshot

_PRODUCT_ID_PATTERN = re.compile(r"[1-9][0-9]{0,18}\Z", re.ASCII)
_PARSER_CONTRACT: Literal["neksio-negative-integer-stock-v1"] = (
    "neksio-negative-integer-stock-v1"
)
_PARSER_BASE_REVISION: Literal["ca5b85e9a3aeaf11ecd8d896eeba95c57e181515"] = (
    "ca5b85e9a3aeaf11ecd8d896eeba95c57e181515"
)
_BATCH_STATUSES = ("candidate", "approved", "paused", "completed", "revoked")
type JsonValue = (
    bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None
)


@dataclass(frozen=True, slots=True)
class CapturedCatalogPage:
    request_body: bytes
    response_body: bytes


@dataclass(frozen=True, slots=True)
class NeksioCatalogCapture:
    feed_id: str
    source_url: str
    started_at: datetime
    finished_at: datetime
    homepage_body: bytes
    pages: tuple[CapturedCatalogPage, ...]


def build_recovery_proposal(
    state: RecoveryPreflightState,
    capture: NeksioCatalogCapture,
) -> NeksioRecoveryProposal:
    """Validate supplied bytes offline; completeness is only relative to those bytes."""
    _validate_capture_identity(capture)
    if capture.feed_id != state.feed_id:
        raise ValueError("capture and recovery state feed IDs differ")
    products, raw_quantities, memberships = _validate_catalog_capture(capture)
    _validate_preflight_state(state)

    snapshots = {snapshot.product_id: snapshot for snapshot in state.snapshots}
    held_ids = {hold.product_id for hold in state.holds}
    delivered_ids = set(state.delivered_ids)
    baselined_ids = set(state.baselined_ids)
    current_ids = {str(product_id) for product_id in products}
    price_ids = set(snapshots) | current_ids | held_ids
    if len(price_ids) > MAX_RECOVERY_PRICE_IDS:
        raise ValueError("recovery price-retention union exceeds limit")
    review_ids = price_ids | delivered_ids | baselined_ids
    if len(review_ids) > MAX_RECOVERY_REVIEW_ITEMS:
        raise ValueError("recovery review union exceeds limit")

    items: list[NeksioRecoveryProposalItem] = []
    for product_id in sorted(review_ids):
        product_number = int(product_id)
        previous_snapshot = snapshots.get(product_id)
        product = products.get(product_number)
        previous = (
            _snapshot_for_proposal(previous_snapshot) if previous_snapshot else None
        )
        target_snapshot = _price_snapshot(state.feed_id, product) if product else None
        target = _snapshot_for_proposal(target_snapshot) if target_snapshot else None
        price_status: Literal["unchanged", "review", "existing_hold", "history_only"]
        if product_id in held_ids:
            price_status = "existing_hold"
        elif previous is not None and target is not None:
            price_status = "unchanged" if previous == target else "review"
        elif previous is not None or target is not None:
            price_status = "review"
        else:
            price_status = "history_only"

        discovery_status: Literal[
            "already_handled",
            "review_existing_unseen",
            "not_observed",
        ]
        if product_id in delivered_ids or product_id in baselined_ids:
            discovery_status = "already_handled"
        elif product is not None:
            discovery_status = "review_existing_unseen"
        else:
            discovery_status = "not_observed"

        context = None
        if product is not None:
            context = _product_context(
                product,
                raw_quantities[product_number],
                memberships[product_number],
            )
        items.append(
            NeksioRecoveryProposalItem(
                product_id=product_id,
                previous=previous,
                target=target,
                context=context,
                price_status=price_status,
                discovery_status=discovery_status,
            ),
        )

    limitations = _limitations(state)
    capture_digest = _capture_digest(capture)
    catalog_digest = _catalog_digest(products, raw_quantities, memberships)
    proposal = NeksioRecoveryProposal(
        version=2,
        parser_contract=_PARSER_CONTRACT,
        parser_base_revision=_PARSER_BASE_REVISION,
        feed_id=capture.feed_id,
        source_url=capture.source_url,
        started_at=capture.started_at,
        finished_at=capture.finished_at,
        state_digest=state.state_digest,
        capture_digest=capture_digest,
        catalog_digest=catalog_digest,
        catalog_count=len(products),
        limitations=limitations,
        items=tuple(items),
        proposal_digest="0" * 64,
    )
    return proposal.model_copy(
        update={
            "proposal_digest": digest(
                proposal.model_dump(mode="json", exclude={"proposal_digest"}),
            ),
        },
    )


def _validate_capture_identity(capture: NeksioCatalogCapture) -> None:
    if not capture.feed_id or len(capture.feed_id) > 256:
        raise ValueError("invalid recovery capture feed ID")
    if capture.source_url != NEKSIO_ORIGIN:
        raise ValueError("recovery capture source must be the exact Neksio origin")
    for timestamp in (capture.started_at, capture.finished_at):
        if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
            raise ValueError("recovery capture times must be aware UTC")
    duration = (capture.finished_at - capture.started_at).total_seconds()
    if duration < 0 or duration > MAX_NEKSIO_SCAN_SECONDS:
        raise ValueError("recovery capture duration exceeds scan deadline")
    if type(capture.homepage_body) is not bytes:
        raise ValueError("recovery homepage must contain raw response bytes")
    if len(capture.homepage_body) > MAX_NEKSIO_RESPONSE_BYTES:
        raise ValueError("recovery homepage exceeds response byte limit")
    if 1 + len(capture.pages) > MAX_NEKSIO_SCAN_RESPONSES:
        raise ValueError("recovery capture exceeds response count limit")
    response_bytes = len(capture.homepage_body)
    for page in capture.pages:
        if (
            type(page.request_body) is not bytes
            or type(page.response_body) is not bytes
        ):
            raise ValueError("captured page bodies must be raw bytes")
        if len(page.response_body) > MAX_NEKSIO_RESPONSE_BYTES:
            raise ValueError("captured page exceeds response byte limit")
        response_bytes += len(page.response_body)
    if response_bytes > MAX_NEKSIO_SCAN_BYTES:
        raise ValueError("recovery capture exceeds aggregate response byte limit")


def _validate_catalog_capture(
    capture: NeksioCatalogCapture,
) -> tuple[dict[int, NeksioProduct], dict[int, int], dict[int, set[int]]]:
    """Check inventory completeness only against the supplied homepage and page bytes."""
    try:
        categories = _category_ids(capture.homepage_body)
    except FeedFetchError as error:
        raise ValueError("invalid captured Neksio homepage") from error
    if len(categories) > MAX_NEKSIO_CATEGORIES:
        raise ValueError("captured Neksio category limit exceeded")

    products: dict[int, NeksioProduct] = {}
    raw_quantities: dict[int, int] = {}
    memberships: dict[int, set[int]] = {}
    cursor = 0
    for category_id in categories:
        if not 1 <= category_id <= MAX_NEKSIO_CATEGORY_ID:
            raise ValueError("invalid captured Neksio category ID")
        first = _captured_page(capture.pages, cursor, category_id, 1)
        first_page, first_raw = first
        declared_pages = first_page.no_of_pages
        page_count = 1 if first_page.no_of_products == 0 else declared_pages
        if page_count < 1 or page_count > MAX_NEKSIO_PAGES_PER_CATEGORY:
            raise ValueError("captured Neksio page limit exceeded")
        category_ids: set[int] = set()
        for page_number in range(1, page_count + 1):
            if page_number == 1:
                page, raw = first_page, first_raw
            else:
                page, raw = _captured_page(
                    capture.pages,
                    cursor,
                    category_id,
                    page_number,
                )
            cursor += 1
            if (
                page.category_id != category_id
                or page.no_of_pages != declared_pages
                or page.no_of_products != first_page.no_of_products
            ):
                raise ValueError("captured Neksio pagination metadata drift")
            raw_cards = raw["productCards"]
            if not isinstance(raw_cards, list):
                raise TypeError("captured Neksio products must be a JSON array")
            for index, card in enumerate(page.product_cards):
                raw_card = raw_cards[index]
                if not isinstance(raw_card, dict):
                    raise TypeError("captured Neksio product must be a JSON object")
                raw_quantity = raw_card["quantity"]
                if type(raw_quantity) is not int:
                    raise TypeError("captured Neksio quantity must be a JSON integer")
                product = card.observe(capture.started_at)
                product_id = product.product_id
                if product_id in category_ids:
                    raise ValueError("duplicate product in captured category")
                category_ids.add(product_id)
                existing = products.get(product_id)
                if existing is not None:
                    if (
                        existing.model_dump(exclude={"observed_at"})
                        != product.model_dump(exclude={"observed_at"})
                        or raw_quantities[product_id] != raw_quantity
                    ):
                        raise ValueError("conflicting cross-category Neksio product")
                else:
                    if len(products) >= MAX_NEKSIO_PRODUCTS:
                        raise ValueError("captured Neksio product limit exceeded")
                    products[product_id] = product
                    raw_quantities[product_id] = raw_quantity
                    memberships[product_id] = set()
                memberships[product_id].add(category_id)
        if len(category_ids) != first_page.no_of_products:
            raise ValueError("captured category unique IDs do not match declared total")

    if cursor != len(capture.pages):
        raise ValueError("captured Neksio pages are missing, repeated, or extra")
    if not products:
        raise ValueError("captured Neksio catalog is empty")
    return products, raw_quantities, memberships


def _captured_page(
    pages: tuple[CapturedCatalogPage, ...],
    index: int,
    category_id: int,
    page_number: int,
) -> tuple[NeksioCatalogPage, dict[str, JsonValue]]:
    if index >= len(pages):
        raise ValueError("captured Neksio page sequence is incomplete")
    captured = pages[index]
    request = _strict_json(captured.request_body)
    expected_request = _catalog_request(category_id, page_number)
    if not _same_json_value(request, cast(JsonValue, expected_request)):
        raise ValueError("captured Neksio request does not match expected page")
    raw_value = _strict_json(captured.response_body)
    if not isinstance(raw_value, dict):
        raise TypeError("captured Neksio page must be a JSON object")
    raw = raw_value
    _require_raw_page_types(raw)
    try:
        page = NeksioCatalogPage.model_validate(
            _offline_page_for_validation(raw),
        )
        _validate_page(page, category_id, page_number)
    except (ValidationError, FeedFetchError) as error:
        raise ValueError("invalid captured Neksio catalog page") from error
    return page, raw


def _offline_page_for_validation(
    raw_page: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Copy a captured page and clamp only stock for the current offline model."""
    page = raw_page.copy()
    raw_cards = raw_page.get("productCards")
    if not isinstance(raw_cards, list):
        raise TypeError("captured Neksio products must be a JSON array")
    normalized_cards: list[JsonValue] = []
    for raw_card in raw_cards:
        if not isinstance(raw_card, dict):
            raise TypeError("captured Neksio product must be a JSON object")
        card = raw_card.copy()
        quantity = card.get("quantity")
        if type(quantity) is not int:
            raise ValueError("captured Neksio quantity must be a JSON integer")
        card["quantity"] = max(quantity, 0)
        normalized_cards.append(card)
    page["productCards"] = normalized_cards
    return page


def _strict_json(body: bytes) -> JsonValue:
    try:
        value = cast(
            JsonValue,
            json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_constant,
            ),
        )
        _reject_nonfinite_numbers(value)
    except (UnicodeDecodeError, ValueError, TypeError):
        raise ValueError("invalid or ambiguous captured JSON") from None
    else:
        return value


def _reject_duplicate_keys(
    pairs: list[tuple[str, JsonValue]],
) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_nonfinite_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _reject_nonfinite_numbers(value: JsonValue) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite JSON number")
    if isinstance(value, dict):
        for child in value.values():
            _reject_nonfinite_numbers(child)
    elif isinstance(value, list):
        for child in value:
            _reject_nonfinite_numbers(child)


def _same_json_value(actual: JsonValue, expected: JsonValue) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            return False
        return actual.keys() == expected.keys() and all(
            _same_json_value(actual[key], expected[key]) for key in expected
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        return len(actual) == len(expected) and all(
            _same_json_value(left, right)
            for left, right in zip(actual, expected, strict=True)
        )
    return actual == expected


def _require_raw_page_types(raw: JsonValue) -> None:
    if not isinstance(raw, dict):
        raise TypeError("captured Neksio page must be a JSON object")
    for name in ("categoryId", "page", "pageSize", "noOfPages", "noOfProducts"):
        if type(raw.get(name)) is not int:
            raise ValueError("captured Neksio pagination values must be JSON integers")
    cards = raw.get("productCards")
    if not isinstance(cards, list):
        raise TypeError("captured Neksio products must be a JSON array")
    for card in cards:
        if not isinstance(card, dict):
            raise TypeError("captured Neksio product must be a JSON object")
        product_id = card.get("productId")
        if type(product_id) is not int:
            raise ValueError("captured Neksio IDs and quantities must be JSON integers")
        quantity = card.get("quantity")
        if type(quantity) is not int:
            raise ValueError("captured Neksio IDs and quantities must be JSON integers")
        if not 1 <= product_id <= MAX_SQLITE_SIGNED_INTEGER:
            raise ValueError("captured Neksio product ID is out of range")
        amount = card.get("priceWTax")
        if type(amount) not in (int, float) or (
            isinstance(amount, float) and not math.isfinite(amount)
        ):
            raise ValueError("captured Neksio price must be a finite JSON number")
        string_fields = (
            "productName",
            "productCode",
            "category",
            "priceWTax_f",
            "imagePath",
        )
        if any(type(card.get(name)) is not str for name in string_fields):
            raise ValueError("captured Neksio display fields must be strings")
        for name in ("subCategory", "manufacturer", "old_PriceWTax"):
            if name in card and card[name] is not None and type(card[name]) is not str:
                raise ValueError("captured Neksio optional display fields are invalid")


def _validate_preflight_state(state: RecoveryPreflightState) -> None:
    if type(state.feed_id) is not str or not state.feed_id:
        raise ValueError("invalid recovery preflight feed ID")
    if len(state.snapshots) > MAX_RECOVERY_PRICE_IDS:
        raise ValueError("recovery snapshots exceed limit")
    if len(state.delivered_ids) > MAX_RECOVERY_PRICE_IDS:
        raise ValueError("recovery delivered history exceeds limit")
    if len(state.holds) > MAX_RECOVERY_PRICE_IDS:
        raise ValueError("recovery holds exceed limit")
    if len(state.baselined_ids) > MAX_RECOVERY_REVIEW_ITEMS:
        raise ValueError("recovery approved baseline exceeds limit")
    for name, value in (
        ("state", state.state_digest),
        ("snapshots", state.snapshots_digest),
        ("history", state.history_digest),
        ("baseline", state.baseline_digest),
        ("holds", state.holds_digest),
    ):
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError(f"invalid recovery {name} digest")
    ids: list[str] = []
    for snapshot in state.snapshots:
        _require_canonical_id(snapshot.product_id)
        if snapshot.feed_id != state.feed_id:
            raise ValueError("recovery snapshot feed ID mismatch")
        if not snapshot.amount.is_finite():
            raise ValueError("recovery snapshot amount is not finite")
        _snapshot_for_proposal(snapshot)
        ids.append(snapshot.product_id)
    if len(ids) != len(set(ids)):
        raise ValueError("recovery snapshots contain duplicate IDs")
    for collection in (state.delivered_ids, state.baselined_ids):
        if len(collection) != len(set(collection)):
            raise ValueError("recovery history contains duplicate IDs")
        for product_id in collection:
            _require_canonical_id(product_id)
    for hold in state.holds:
        _require_canonical_id(hold.product_id)
        if hold.kind not in {"review", "availability"}:
            raise ValueError("unsupported recovery hold kind")
    if len({hold.product_id for hold in state.holds}) != len(state.holds):
        raise ValueError("recovery holds contain duplicate IDs")
    if state.baseline_candidate is not None:
        if state.baseline_candidate.feed_id != state.feed_id:
            raise ValueError("recovery candidate feed ID mismatch")
        if len(state.baseline_candidate.entry_ids) != len(
            set(state.baseline_candidate.entry_ids),
        ):
            raise ValueError("recovery candidate contains duplicate IDs")
        if len(state.baseline_candidate.entry_ids) > MAX_RECOVERY_REVIEW_ITEMS:
            raise ValueError("recovery baseline candidate exceeds limit")
        if state.baseline_candidate.status not in {"candidate", "approved"}:
            raise ValueError("unsupported recovery baseline candidate status")
        for product_id in state.baseline_candidate.entry_ids:
            _require_canonical_id(product_id)
    if state.normal_cursor is not None:
        _require_canonical_id(state.normal_cursor[0])
        if type(state.normal_cursor[1]) is not int:
            raise ValueError("invalid recovery normal-delivery cursor timestamp")
    if len(state.batch_counts) != len(_BATCH_STATUSES) or any(
        type(count) is not int or count < 0 for count in state.batch_counts
    ):
        raise ValueError("invalid recovery batch diagnostics")


def _require_canonical_id(product_id: str) -> None:
    if (
        type(product_id) is not str
        or _PRODUCT_ID_PATTERN.fullmatch(product_id) is None
        or int(product_id) > MAX_SQLITE_SIGNED_INTEGER
    ):
        raise ValueError("recovery product IDs must be canonical positive decimal IDs")


def _snapshot_for_proposal(
    snapshot: PriceSnapshot | None,
) -> ReconciliationSnapshot | None:
    if snapshot is None:
        return None
    if (
        not isinstance(snapshot.amount, Decimal)
        or not snapshot.amount.is_finite()
        or snapshot.amount < 0
    ):
        raise ValueError("recovery price snapshot amount must be finite")
    result = ReconciliationSnapshot.from_snapshot(snapshot)
    if (
        not result.formatted
        or len(result.formatted) > 128
        or result.formatted != result.formatted.strip()
        or any(
            ord(character) < 32 or ord(character) == 127
            for character in result.formatted
        )
    ):
        raise ValueError("recovery price snapshot display is invalid")
    return result


def _price_snapshot(feed_id: str, product: NeksioProduct) -> PriceSnapshot:
    return PriceSnapshot(
        feed_id=feed_id,
        product_id=str(product.product_id),
        amount=Decimal(str(product.price_with_tax)),
        formatted=product.formatted_price,
        currency="MKD",
    )


def _full_product_projection(
    product: NeksioProduct,
    raw_quantity: int,
) -> dict[str, JsonValue]:
    return {
        "product_id": str(product.product_id),
        "product_name": product.product_name,
        "product_code": product.product_code,
        "category": product.category,
        "subcategory": product.subcategory,
        "manufacturer": product.manufacturer,
        "amount": canonicalize_price_amount(product.price_with_tax),
        "formatted_price": product.formatted_price,
        "old_formatted_price": product.old_formatted_price,
        "stock_quantity": product.stock_quantity,
        "raw_stock_quantity": raw_quantity,
        "image_path": product.image_path,
    }


def _product_context(
    product: NeksioProduct,
    raw_quantity: int,
    category_ids: set[int],
) -> str:
    context = _full_product_projection(product, raw_quantity)
    context["category_ids"] = cast(JsonValue, sorted(category_ids))
    context["source_context_digest"] = digest(context)
    context.pop("image_path")
    return canonical_json(context)


def _catalog_digest(
    products: dict[int, NeksioProduct],
    raw_quantities: dict[int, int],
    memberships: dict[int, set[int]],
) -> str:
    projection: list[dict[str, JsonValue]] = []
    for product_id in sorted(products, key=str):
        product = products[product_id]
        item = _full_product_projection(product, raw_quantities[product_id])
        item["category_ids"] = cast(JsonValue, sorted(memberships[product_id]))
        projection.append(item)
    return digest(projection)


def _capture_digest(capture: NeksioCatalogCapture) -> str:
    return digest(
        {
            "provider": "neksio",
            "feed_id": capture.feed_id,
            "source_url": capture.source_url,
            "parser_contract": _PARSER_CONTRACT,
            "parser_base_revision": _PARSER_BASE_REVISION,
            "homepage_sha256": hashlib.sha256(capture.homepage_body).hexdigest(),
            "pages": tuple(
                {
                    "request_sha256": hashlib.sha256(page.request_body).hexdigest(),
                    "response_sha256": hashlib.sha256(page.response_body).hexdigest(),
                }
                for page in capture.pages
            ),
        },
    )


def _limitations(state: RecoveryPreflightState) -> tuple[str, ...]:
    limitations = [
        "offline_evidence_not_authenticated",
        "not_an_application_plan",
    ]
    if state.initialized_at is None:
        limitations.append("uninitialized_feed")
    if any(state.batch_counts):
        limitations.append("price_batch_history")
    if state.foreign_provider_batch_count:
        limitations.append("foreign_provider_batch")
    if state.open_claim_count:
        limitations.append("open_price_claims")
    if state.reconciliation_count:
        limitations.append("prior_reconciliation")
    if state.hold_release_count:
        limitations.append("hold_release_history")
    if any(hold.kind == "availability" for hold in state.holds):
        limitations.append("availability_holds")
    if (
        state.baseline_candidate is not None
        and state.baseline_candidate.status == "candidate"
    ):
        limitations.append("pending_baseline_candidate")
    if state.baseline_state is not None and state.baseline_state[1]:
        limitations.append("existing_complete_baseline")
    return tuple(limitations)
