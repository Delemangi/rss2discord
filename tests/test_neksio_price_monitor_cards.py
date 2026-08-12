from pathlib import Path

from rss2discord.delivery_store import DeliveryStore
from tests.discord_components_helpers import get_all_components
from tests.neksio_price_monitor_helpers import (
    CatalogStub,
    RecordingSender,
    make_feed,
    make_monitor,
    make_product,
)


def test_price_alert_without_image_omits_thumbnail(tmp_path: Path) -> None:
    # Given
    before = make_product(1, amount="100", formatted="100 MKD").model_copy(
        update={"image_path": ""},
    )
    after = make_product(1, amount="90", formatted="90 MKD").model_copy(
        update={"image_path": ""},
    )
    sender = RecordingSender([True])

    # When
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            make_feed(),
            CatalogStub([(before,), (after,)]),
            store,
            sender,
        )
        monitor.scan()
        monitor.scan()

    # Then
    message = sender.messages[0]
    assert message.entry.image_url is None
    assert all(component.get("type") != 11 for component in get_all_components(message))
