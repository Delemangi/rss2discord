"""Stock availability reaches the snapshots of stock-aware providers."""

from collections.abc import Callable, Sequence
from decimal import Decimal
from pathlib import Path

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.models import PriceSnapshot
from rss2discord.providers.ananas.models import AnanasProduct
from rss2discord.providers.ananas.prices import AnanasPriceMonitor
from rss2discord.providers.cccenter.models import CCCenterListing, CCCenterProduct
from rss2discord.providers.cccenter.prices import CCCenterPriceMonitor
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
    PriceObservation,
)
from tests.anhoch_price_monitor_helpers import CatalogStub as AnhochCatalogStub
from tests.anhoch_price_monitor_helpers import make_monitor as make_anhoch_monitor
from tests.anhoch_price_monitor_helpers import make_product as make_anhoch_product
from tests.setec_price_monitor_helpers import RecordingSender


class AnanasCatalogStub:
    def __init__(self, products: tuple[AnanasProduct, ...]) -> None:
        self._products = products

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[AnanasProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._products


class CCCenterCatalogStub:
    def __init__(self, products: tuple[CCCenterProduct, ...]) -> None:
        self._products = products

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[CCCenterProduct, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._products

    def fetch_product_details(
        self,
        listings: Sequence[CCCenterListing | CCCenterProduct],
        *,
        is_shutdown_requested: Callable[[], bool] = lambda: False,
    ) -> tuple[CCCenterProduct, ...]:
        del is_shutdown_requested
        by_id = {product.product_id: product for product in self._products}
        return tuple(by_id[listing.product_id] for listing in listings)


def ananas_product(
    *,
    product_id: str,
    in_stock: bool,
    price: int = 1000,
) -> AnanasProduct:
    return AnanasProduct(
        inventory_id="1",
        product_id=product_id,
        name=f"Product {product_id}",
        url=f"https://ananas.mk/proizvod/{product_id}/1",
        image_url=None,
        brand=None,
        categories=("IT Shop",),
        price=Decimal(price),
        base_price=None,
        currency="MKD",
        in_stock=in_stock,
        available=0 if not in_stock else 2,
        merchant="Ananas Shop",
        first_published_ms=None,
        has_variations=False,
    )


def cc_center_product(
    *,
    product_id: str,
    in_stock: bool,
    price: str = "100",
) -> CCCenterProduct:
    return CCCenterProduct(
        product_id=product_id,
        name=f"Product {product_id}",
        url=product_id,
        sku="A-1",
        current_price=Decimal(price),
        original_price=None,
        image_url=None,
        categories=("Лаптопи",),
        is_in_stock=in_stock,
    )


def dependencies[CatalogT](
    catalog: CatalogT,
    store: DeliveryStore,
    sender: RecordingSender,
) -> PriceMonitorDependencies[CatalogT]:
    return PriceMonitorDependencies(
        catalog=catalog,
        snapshots=store,
        sender=sender,
        fetch_retry_policy=FetchRetryPolicy(
            sleep=lambda _: True,
            on_retry=lambda *_: None,
        ),
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


def ananas_feed() -> FeedConfig:
    return FeedConfig(
        id="ananas",
        url="https://ananas.mk/kategorii/it-shop/kompjuterski-komponenti",
        webhook="https://discord.example.test/hook",
        strategy="ananas",
    )


def cc_center_feed() -> FeedConfig:
    return FeedConfig(
        id="cccenter",
        url="https://cccenter.mk/shop/?orderby=date",
        webhook="https://discord.example.test/hook",
        strategy="cccenter",
    )


def test_ananas_observes_out_of_stock_products_with_a_valid_price(
    tmp_path: Path,
) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = AnanasPriceMonitor(
            ananas_feed(),
            dependencies(
                AnanasCatalogStub(
                    (
                        ananas_product(product_id="alpha", in_stock=False, price=1000),
                        ananas_product(product_id="beta", in_stock=True, price=500),
                    ),
                ),
                store,
                RecordingSender([]),
            ),
        )

        monitor.scan()

        persisted = {
            item.product_id: item for item in store.load_price_snapshots("ananas")
        }

    assert persisted["alpha"].amount == Decimal(1000)
    assert persisted["alpha"].available is False
    assert persisted["beta"].available is True


def test_anhoch_snapshot_reports_availability(tmp_path: Path) -> None:
    product = make_anhoch_product(1, amount="1000", formatted="1.000 ден")
    out_of_stock = product.model_copy(update={"is_in_stock": False})

    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_anhoch_monitor(
            FeedConfig(
                id="anhoch",
                url="https://www.anhoch.com/products",
                webhook="https://discord.example.test/hook",
                strategy="anhoch",
            ),
            AnhochCatalogStub([(out_of_stock,)]),
            store,
            RecordingSender([]),
        )

        monitor.scan()

        persisted = store.load_price_snapshots("anhoch")

    assert persisted[0].available is False


def test_cccenter_snapshot_reports_availability(tmp_path: Path) -> None:
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = CCCenterPriceMonitor(
            cc_center_feed(),
            dependencies(
                CCCenterCatalogStub(
                    (cc_center_product(product_id="a", in_stock=False),),
                ),
                store,
                RecordingSender([]),
            ),
        )

        monitor.scan()

        persisted = store.load_price_snapshots("cccenter")

    assert persisted[0].available is False


def test_cccenter_does_not_confirm_a_restock_that_details_call_unavailable(
    tmp_path: Path,
) -> None:
    product_id = "https://cccenter.mk/product/a/"
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = CCCenterPriceMonitor(
            cc_center_feed(),
            dependencies(
                CCCenterCatalogStub(
                    (cc_center_product(product_id=product_id, in_stock=False),),
                ),
                store,
                RecordingSender([]),
            ),
        )
        observation = PriceObservation(
            cc_center_product(product_id=product_id, in_stock=False),
            PriceSnapshot(
                "cccenter",
                product_id,
                Decimal(100),
                "100 ден",
                "MKD",
                available=True,
            ),
        )

        assert monitor._confirm_observations((observation,)) == []


def test_cccenter_confirms_a_restock_when_details_agree(tmp_path: Path) -> None:
    product_id = "https://cccenter.mk/product/a/"
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = CCCenterPriceMonitor(
            cc_center_feed(),
            dependencies(
                CCCenterCatalogStub(
                    (cc_center_product(product_id=product_id, in_stock=True),),
                ),
                store,
                RecordingSender([]),
            ),
        )
        observation = PriceObservation(
            cc_center_product(product_id=product_id, in_stock=True),
            PriceSnapshot(
                "cccenter",
                product_id,
                Decimal(100),
                "100 ден",
                "MKD",
                available=True,
            ),
        )

        confirmed = monitor._confirm_observations((observation,))

    assert [item.snapshot.product_id for item in confirmed] == [product_id]
