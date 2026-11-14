"""Durable outbox and detailed digest delivery, using local SQLite and fake senders."""

import csv
import io
import json
import sqlite3
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from rss2discord.delivery_store import DeliveryStore, PriceSnapshot
from rss2discord.discord import report as report_module
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.discord.components import TEXT_DISPLAY_COMPONENT
from rss2discord.discord.message import ReportFile, prepare_delivery
from rss2discord.models import EntryData, PriceDirection, SourceMetric
from rss2discord.price_notifications import (
    PriceNotification,
    PriceNotificationDeliveryResult,
    PriceNotificationRecord,
    entry_from_json,
    entry_to_json,
)
from rss2discord.retries import SQLiteRetryPolicy
from rss2discord.transports.price_monitor import PriceAlertDelivery
from rss2discord.transports.price_monitor.digest import (
    build_digest_messages,
    build_report_files,
    deliver_price_notifications,
)
from tests import setec_price_monitor_helpers as setec


def snapshot(
    product_id: str,
    amount: int = 90,
    *,
    feed_id: str = "feed",
) -> PriceSnapshot:
    return PriceSnapshot(feed_id, product_id, Decimal(amount), str(amount), "MKD")


def entry(
    product_id: str,
    *,
    description: str = "",
    image_url: str | None = None,
    categories: tuple[str, ...] = ("Computers", "Accessories"),
) -> EntryData:
    return EntryData(
        title=f"Product {product_id}",
        link=f"https://shop.example.test/{product_id}",
        description=description,
        author="Shop",
        timestamp="2026-11-12T10:00:00+00:00",
        discussion_url="https://shop.example.test/talk",
        image_url=image_url,
        categories=categories,
        source_metrics=(
            SourceMetric("Price", "90 MKD"),
            SourceMetric("Previous", "100 MKD", prior=True),
        ),
        price_direction=PriceDirection.DECREASE,
    )


def notification(
    product_id: str,
    *,
    description: str = "",
    image_url: str | None = None,
) -> PriceNotification:
    item_entry = entry(product_id, description=description, image_url=image_url)
    return PriceNotification(
        product_id=product_id,
        previous=snapshot(product_id, 100),
        current=snapshot(product_id, 90),
        entry=item_entry,
        source_title="Test Shop",
    )


def record(
    product_id: str,
    *,
    notification_id: int = 1,
    feed_id: str = "feed",
    group_id: str = "group-a",
    title: str = "Product",
    link: str = "https://shop.example.test/item",
    previous_price: str | None = "100",
    current_price: str = "90",
    description: str = "",
    image_url: str | None = None,
    categories: tuple[str, ...] = ("Computers", "Accessories"),
    observed_at: int = 1_700_000_000,
    attempts: int = 0,
) -> PriceNotificationRecord:
    return PriceNotificationRecord(
        notification_id=notification_id,
        feed_id=feed_id,
        group_id=group_id,
        product_id=product_id,
        title=title,
        link=link,
        previous_price=previous_price,
        current_price=current_price,
        previous_amount=(None if previous_price is None else Decimal(previous_price)),
        current_amount=Decimal(current_price),
        currency="MKD",
        entry=entry(
            product_id,
            description=description,
            image_url=image_url,
            categories=categories,
        ),
        source_title="Test Shop",
        observed_at=observed_at,
        attempts=attempts,
    )


def record_scan(
    store: DeliveryStore,
    product_ids: tuple[str, ...],
    *,
    group_id: str = "group-a",
    description: str = "",
    observed_at: int = 1_700_000_000,
) -> int:
    notifications = tuple(
        notification(product_id, description=description) for product_id in product_ids
    )
    return store.record_price_scan(
        feed_id="feed",
        group_id=group_id,
        observed_at=observed_at,
        baseline_updates=tuple(item.current for item in notifications),
        notifications=notifications,
    )


def test_record_price_scan_is_atomic_and_restart_keeps_pending(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        count = record_scan(store, ("1", "2"))
        assert count == 2
        assert store.count_pending_price_notifications("feed") == 2
        assert {item.product_id for item in store.load_price_snapshots("feed")} == {
            "1",
            "2",
        }
    with DeliveryStore(path) as reopened:
        group = reopened.load_pending_price_group("feed")
        assert group is not None
        assert group[0] == "group-a"
        assert [item.product_id for item in group[1]] == ["1", "2"]
        assert group[1][0].entry == entry("1")
        assert group[1][0].current_price == "90"
        assert group[1][0].previous_price == "100"
        assert group[1][0].attempts == 0


def test_record_price_scan_rolls_back_baseline_with_notifications(
    tmp_path: Path,
) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TRIGGER fail_notification BEFORE INSERT ON price_notifications "
                "BEGIN SELECT RAISE(ABORT, 'fixture insert failure'); END",
            )
        with pytest.raises(sqlite3.IntegrityError, match="fixture insert failure"):
            record_scan(store, ("1",))
        assert store.load_price_snapshots("feed") == ()
        assert store.count_pending_price_notifications("feed") == 0


def test_pending_groups_load_oldest_only_and_are_marked_sent(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        record_scan(store, ("1", "2"), group_id="older", observed_at=10)
        record_scan(store, ("3",), group_id="newer", observed_at=20)
        first = store.load_pending_price_group("feed")
        assert first is not None
        assert first[0] == "older"
        assert [item.product_id for item in first[1]] == ["1", "2"]
        store.mark_price_notifications_sent(item.notification_id for item in first[1])
        second = store.load_pending_price_group("feed")
        assert second is not None
        assert second[0] == "newer"
        assert [item.product_id for item in second[1]] == ["3"]
        store.mark_price_notifications_sent(item.notification_id for item in second[1])
        assert store.load_pending_price_group("feed") is None
        assert store.count_pending_price_notifications("feed") == 0


def test_attempt_records_sanitized_bounded_error(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        record_scan(store, ("1",))
        group = store.load_pending_price_group("feed")
        assert group is not None
        ids = [item.notification_id for item in group[1]]
        store.record_price_notification_attempt(ids, "bad\nline\t" + "x" * 400)
        loaded = store.load_pending_price_group("feed")
        assert loaded is not None
        assert loaded[1][0].attempts == 1
        row = store._connection.execute(
            "SELECT last_attempt_at, last_error FROM price_notifications",
        ).fetchone()
        assert row is not None
        assert row[0] is not None
        assert "\n" not in row[1]
        assert len(row[1]) == 200


def test_read_only_connection_can_read_pending_rows(tmp_path: Path) -> None:
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        record_scan(store, ("1",))
    with DeliveryStore(path, read_only=True) as reader:
        group = reader.load_pending_price_group("feed")
        assert group is not None
        assert group[0] == "group-a"
        assert reader.count_pending_price_notifications("feed") == 1


def test_confirmation_subset_rotates_and_wraps(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        assert store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("3", "1", "2"),
            limit=2,
        ) == ("1", "2")
        assert store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("3", "1", "2"),
            limit=2,
        ) == ("3", "1")
        assert store.select_price_confirmation_subset(
            feed_id="feed",
            product_ids=("3", "1", "2"),
            limit=2,
        ) == ("2", "3")


def test_setec_small_scan_sends_every_change_without_a_ten_cap(tmp_path: Path) -> None:
    ids = tuple(f"p-{index:02}" for index in range(25))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED] * 25)
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, changed, changed]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 25
        assert store.count_pending_price_notifications("setec") == 0
        assert all(item.amount == 90 for item in store.load_price_snapshots("setec"))


def test_setec_bulk_scan_sends_stats_digest_with_reports(tmp_path: Path) -> None:
    ids = tuple(f"p-{index:02}" for index in range(26))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, changed, changed]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert len(message.files) == 2
        assert message.files[0].filename.endswith(".html")
        assert message.files[1].filename.endswith(".csv")
        assert "Largest drops" in message.entry.description
        csv_text = message.files[1].content.decode()
        assert len(list(csv.reader(io.StringIO(csv_text)))) == 27
        assert all(pid in csv_text for pid in ids)
        assert store.count_pending_price_notifications("setec") == 0
        assert all(item.amount == 90 for item in store.load_price_snapshots("setec"))


def test_large_scan_sends_stats_digest_with_html_and_csv(tmp_path: Path) -> None:
    ids = tuple(f"p-{index:03}" for index in range(201))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, changed, changed]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert len(message.files) == 2
        assert message.files[0].filename.endswith(".html")
        assert message.files[1].filename.endswith(".csv")
        html_text = message.files[0].content.decode()
        csv_text = message.files[1].content.decode()
        assert "<html" in html_text
        assert "Largest drops" in message.entry.description
        assert len(list(csv.reader(io.StringIO(csv_text)))) == 202
        assert all(pid in csv_text for pid in ids)
        assert store.count_pending_price_notifications("setec") == 0
        assert all(item.amount == 90 for item in store.load_price_snapshots("setec"))


def test_digest_payload_aligns_attachments_files_and_file_components() -> None:
    records = tuple(
        record(
            f"p-{index}",
            notification_id=index + 1,
            group_id="abcdef123456",
        )
        for index in range(26)
    )
    message = build_digest_messages(setec.make_feed(), records)[0]
    assert message.entry.title == "Price changes"

    class NoDownload:
        def download(self, url: str) -> None:
            raise AssertionError(f"digest must not download {url}")

    prepared = prepare_delivery(message, NoDownload(), lambda _: True)  # type: ignore[arg-type]
    payload = prepared.request.payload
    attachments = payload["attachments"]
    assert isinstance(attachments, list)
    assert [item["filename"] for item in attachments if isinstance(item, dict)] == [
        report.filename for report in message.files
    ]
    assert [item["id"] for item in attachments if isinstance(item, dict)] == [0, 1]
    assert [report.filename for report in prepared.request.files] == [
        report.filename for report in message.files
    ]

    class FakeSession:
        def post(self, url: str, **kwargs: object) -> object:
            self.url = url
            self.kwargs = kwargs
            return object()

    session = FakeSession()
    prepared.request.post(session)  # type: ignore[arg-type]
    multipart = session.kwargs["files"]
    assert isinstance(multipart, dict)
    assert list(multipart) == ["files[0]", "files[1]"]
    assert [multipart[key][0] for key in multipart] == [
        item["filename"] for item in attachments if isinstance(item, dict)
    ]
    components = payload["components"]
    assert isinstance(components, list)
    file_components = [
        component
        for component in components
        if isinstance(component, dict) and component.get("type") == 13
    ]

    def file_url(component: object) -> str | None:
        if not isinstance(component, dict):
            return None
        file = component.get("file")
        if not isinstance(file, dict):
            return None
        url = file.get("url")
        return url if isinstance(url, str) else None

    assert [
        url for component in file_components if (url := file_url(component)) is not None
    ] == [f"attachment://{report.filename}" for report in message.files]
    assert payload["flags"] == 1 << 15
    assert payload["allowed_mentions"] == {"parse": []}
    assert prepared.fallback_request is None


def test_digest_respects_component_and_text_limits_and_has_no_images() -> None:
    records = tuple(
        record(
            f"p-{index}",
            notification_id=index + 1,
            group_id="group-long",
            description="long description " * 200,
            image_url=f"https://images.example.test/{index}.png",
        )
        for index in range(26)
    )
    message = build_digest_messages(setec.make_feed(), records)[0]

    class NoDownload:
        def download(self, url: str) -> None:
            raise AssertionError(f"digest must not download {url}")

    payload = prepare_delivery(message, NoDownload(), lambda _: True).request.payload  # type: ignore[arg-type]
    components = payload["components"]
    assert isinstance(components, list)
    assert len(components) <= 40
    assert "https://images.example.test" not in json.dumps(payload)

    def text_length(items: Sequence[object]) -> int:
        total = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if item.get("type") == TEXT_DISPLAY_COMPONENT and isinstance(content, str):
                total += len(content)
            children = item.get("components")
            if isinstance(children, list):
                total += text_length(children)
        return total

    assert text_length(components) <= 4000


def test_brief_preserves_decimal_prices_and_exact_percentage() -> None:
    records = (
        record(
            "decimal",
            notification_id=1,
            previous_price="100.4",
            current_price="100.6",
        ),
    )
    message = build_digest_messages(setec.make_feed(), records)[0]
    assert "100.4" in message.entry.description
    assert "100.6" in message.entry.description
    assert "+0.2%" in message.entry.description
    assert "100 →" not in message.entry.description
    assert "→ 101" not in message.entry.description


def test_additional_pending_note_appears_only_when_present() -> None:
    records = tuple(
        record(str(index), notification_id=index + 1) for index in range(26)
    )
    with_pending = build_digest_messages(
        setec.make_feed(),
        records,
        additional_pending=22,
    )[-1]
    assert "22 additional changes are pending confirmation" in (
        with_pending.entry.description
    )
    without_pending = build_digest_messages(setec.make_feed(), records)[-1]
    assert "additional changes are pending confirmation" not in (
        without_pending.entry.description
    )


def test_delivery_threads_additional_pending_into_brief(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        record_scan(store, tuple(str(index) for index in range(26)))
        sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
        result = deliver_price_notifications(
            _digest_dependencies(store, sender),
            setec.make_feed(),
            "feed",
            additional_pending=22,
        )
        assert result is PriceNotificationDeliveryResult.DRAINED
        assert "22 additional changes are pending confirmation" in (
            sender.messages[0].entry.description
        )


def test_report_count_placeholder_in_title_survives() -> None:
    records = (
        record(
            "count",
            notification_id=1,
            title="{count} bargain",
        ),
    )
    html_report, _csv_report = build_report_files("feed", "group", records)
    html_text = html_report.content.decode()
    assert "{count} bargain" in html_text
    assert '<span class="note">1 rows</span>' in html_text


def test_mixed_drops_and_rises_send_two_brief_messages() -> None:
    records = (
        *(
            record(
                f"drop-{index}",
                notification_id=index + 1,
                previous_price="100",
                current_price="90",
                categories=(f"Category {index % 5}",),
            )
            for index in range(20)
        ),
        *(
            record(
                f"rise-{index}",
                notification_id=100 + index,
                previous_price="100",
                current_price="110",
                categories=(f"Category {index % 5}",),
            )
            for index in range(10)
        ),
        record(
            "extreme",
            notification_id=200,
            previous_price="100",
            current_price="1100",
        ),
    )
    messages = build_digest_messages(setec.make_feed(), records)
    assert len(messages) == 2
    drops, rises = messages
    assert drops.files == ()
    assert "**Largest drops**" in drops.entry.description
    assert "**Largest rises**" not in drops.entry.description
    assert "████" in drops.entry.description
    assert "-10.0%" in drops.entry.description
    assert "+10.0%" in rises.entry.description
    assert rises.files
    assert len(rises.files) == 2
    assert "**Largest rises**" in rises.entry.description
    assert "extreme moves" in rises.entry.description
    assert "The complete searchable list is attached (HTML and CSV)." in (
        rises.entry.description
    )
    assert all(len(message.entry.description) <= 4000 for message in messages)
    assert drops.entry.description.count("→") == 20
    assert rises.entry.description.count("→") == 10


def test_all_drops_attach_reports_to_single_message() -> None:
    records = tuple(
        record(
            str(index),
            notification_id=index + 1,
            previous_price="100",
            current_price="90",
        )
        for index in range(30)
    )
    messages = build_digest_messages(setec.make_feed(), records)
    assert len(messages) == 1
    assert len(messages[0].files) == 2
    assert "**Largest drops**" in messages[0].entry.description
    assert "The complete searchable list is attached (HTML and CSV)." in (
        messages[0].entry.description
    )


def _digest_dependencies(
    store: DeliveryStore,
    sender: setec.RecordingSender,
) -> SimpleNamespace:
    return SimpleNamespace(
        snapshots=store,
        sender=sender,
        sqlite_retry_policy=SQLiteRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
        delivery=PriceAlertDelivery(
            sleep=lambda _: True,
            delay_between_posts=0,
            is_shutdown_requested=lambda: False,
        ),
    )


def _mixed_scan(store: DeliveryStore) -> None:
    notifications = tuple(
        PriceNotification(
            product_id=f"p-{index}",
            previous=PriceSnapshot("feed", f"p-{index}", Decimal(100), "100", "MKD"),
            current=PriceSnapshot(
                "feed",
                f"p-{index}",
                Decimal(90 if index % 2 == 0 else 110),
                "90" if index % 2 == 0 else "110",
                "MKD",
            ),
            entry=entry(f"p-{index}"),
            source_title="Test Shop",
        )
        for index in range(26)
    )
    store.record_price_scan(
        feed_id="feed",
        group_id="mixed",
        observed_at=1_700_000_000,
        baseline_updates=tuple(item.current for item in notifications),
        notifications=notifications,
    )


def test_two_message_digest_marks_all_rows_after_both_sends(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _mixed_scan(store)
        sender = setec.RecordingSender(
            [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.DELIVERED],
        )
        result = deliver_price_notifications(
            _digest_dependencies(store, sender),
            setec.make_feed(),
            "feed",
        )
        assert result is PriceNotificationDeliveryResult.DRAINED
        assert len(sender.messages) == 2
        assert store.count_pending_price_notifications("feed") == 0


def test_failed_second_digest_message_leaves_all_rows_pending(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        _mixed_scan(store)
        sender = setec.RecordingSender(
            [DiscordDeliveryResult.DELIVERED, DiscordDeliveryResult.FAILED],
        )
        result = deliver_price_notifications(
            _digest_dependencies(store, sender),
            setec.make_feed(),
            "feed",
        )
        assert result is PriceNotificationDeliveryResult.FAILED
        assert len(sender.messages) == 2
        pending = store.load_pending_price_group("feed")
        assert pending is not None
        assert len(pending[1]) == 26
        assert all(item.attempts == 1 for item in pending[1])


def test_failed_large_digest_keeps_rows_pending_and_retry_reuses_them(
    tmp_path: Path,
) -> None:
    ids = tuple(f"p-{index:03}" for index in range(201))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    changed = tuple(setec.make_product(pid, calculated_amount=90) for pid in ids)
    sender = setec.RecordingSender(
        [DiscordDeliveryResult.FAILED, DiscordDeliveryResult.DELIVERED],
    )
    path = tmp_path / "state.db"
    with DeliveryStore(path) as store:
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline, changed, changed]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()
        pending = store.load_pending_price_group("setec")
        assert pending is not None
        first_files = pending[1]
        assert len(first_files) == 201
        assert all(item.attempts == 1 for item in first_files)
        assert store.list_health("setec")[0].cause == "DeliveryRetryPending"
        assert all(item.amount == 90 for item in store.load_price_snapshots("setec"))
        monitor.scan()
        assert len(sender.messages) == 2
        assert store.count_pending_price_notifications("setec") == 0
        first_digest, retry_digest = sender.messages
        assert first_digest.files[1].content == retry_digest.files[1].content
        assert store.list_health("setec")[0].state == "healthy"


def test_csv_and_html_escape_formulas_and_newlines() -> None:
    records = (
        record(
            "1",
            notification_id=1,
            group_id="group-escape",
            title="=SUM(A1:A2)",
            link="https://shop.example.test/item?a=1",
            previous_price=None,
            current_price="+90",
        ),
        record(
            "2",
            notification_id=2,
            group_id="group-escape",
            title="a|b\nc",
            link="https://shop.example.test/item?b=2",
        ),
    )
    html_report, csv_report = build_report_files("feed", "group-escape", records)
    csv_text = csv_report.content.decode()
    rows = list(csv.reader(io.StringIO(csv_text)))
    assert rows[1][2] == "'=SUM(A1:A2)"
    assert rows[2][2] == "a|b\nc"
    assert rows[0][-1] == "observed_at"
    html_text = html_report.content.decode()
    assert "=SUM(A1:A2)" in html_text
    assert "a|b c" in html_text
    assert html_text.startswith("<!doctype html>")


def test_entry_json_round_trip_is_deterministic() -> None:
    original = entry(
        "42",
        description="line one\nline two",
        image_url="https://img/p.png",
    )
    encoded = entry_to_json(original)
    assert encoded == entry_to_json(original)
    assert entry_from_json(encoded) == original


def test_truncation_note_is_written_when_report_rows_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(report_module, "MAX_REPORT_ROWS", 2)
    records = tuple(
        record(str(index), notification_id=index + 1, group_id="group-cap")
        for index in range(4)
    )
    html_report, csv_report = build_report_files("feed", "group-cap", records)
    assert "TRUNCATED: 2 rows omitted" in html_report.content.decode()
    assert "TRUNCATED: 2 rows omitted" in csv_report.content.decode()


def test_truncation_note_is_written_when_report_characters_are_capped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(report_module, "MAX_REPORT_CHARACTERS", 20)
    records = tuple(
        record(str(index), notification_id=index + 1, group_id="group-chars")
        for index in range(3)
    )
    _html_report, csv_report = build_report_files("feed", "group-chars", records)
    assert "TRUNCATED: 3 rows omitted" in csv_report.content.decode()


def test_pending_previous_run_is_drained_before_new_small_scan(tmp_path: Path) -> None:
    ids = tuple(f"p-{index:02}" for index in range(1))
    baseline = tuple(setec.make_product(pid, calculated_amount=100) for pid in ids)
    sender = setec.RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        store.record_price_scan(
            feed_id="setec",
            group_id="previous-run",
            observed_at=1_700_000_000,
            baseline_updates=tuple(
                PriceSnapshot("setec", pid, Decimal(100), "100 ден.", "MKD")
                for pid in ids
            ),
            notifications=tuple(
                PriceNotification(
                    product_id=pid,
                    previous=PriceSnapshot("setec", pid, Decimal(90), "90 ден.", "MKD"),
                    current=PriceSnapshot(
                        "setec",
                        pid,
                        Decimal(100),
                        "100 ден.",
                        "MKD",
                    ),
                    entry=entry(pid),
                    source_title="Setec Deals",
                )
                for pid in ids
            ),
        )
        monitor = setec.make_monitor(
            setec.make_feed(),
            setec.CatalogStub([baseline]),
            store,
            sender,
        )
        monitor.scan()
        assert len(sender.messages) == 1
        assert store.count_pending_price_notifications("setec") == 0


def test_report_file_dataclass_carries_explicit_content_type() -> None:
    report = ReportFile(
        filename="report.csv",
        content=b"a,b\n",
        content_type="text/csv",
    )
    assert report.content == b"a,b\n"
    assert report.content_type == "text/csv"
