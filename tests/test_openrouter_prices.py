"""OpenRouter significant price-drop monitor behavior."""

from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from rss2discord.configuration import FeedConfig
from rss2discord.delivery_store import DeliveryStore
from rss2discord.discord.client import DiscordDeliveryResult
from rss2discord.models import PriceDirection
from rss2discord.providers.openrouter_models.bounds import OPENROUTER_MODELS_URL
from rss2discord.providers.openrouter_models.models import OpenRouterModel
from rss2discord.providers.openrouter_models.prices import OpenRouterPriceMonitor
from rss2discord.retries import FetchRetryPolicy, SQLiteRetryPolicy
from rss2discord.transports.price_monitor import (
    PriceAlertDelivery,
    PriceMonitorDependencies,
)
from tests.setec_price_monitor_helpers import RecordingSender


def make_model(
    *,
    model_id: str = "vendor/model",
    name: str = "Vendor: Model",
    prompt: str = "0.000001",
    completion: str = "0.000002",
    cache: str = "0.0000005",
    tools: bool = True,
    hugging_face_id: str | None = "vendor/model",
) -> OpenRouterModel:
    return OpenRouterModel.model_validate(
        {
            "id": model_id,
            "name": name,
            "created": 1_794_938_432,
            "description": "A model for testing.",
            "context_length": 1_048_576,
            "architecture": {"modality": "text->text"},
            "pricing": {
                "prompt": prompt,
                "completion": completion,
                "input_cache_read": cache,
            },
            "top_provider": {"max_completion_tokens": 65_536},
            "supported_parameters": ["tools"] if tools else ["temperature"],
            "expiration_date": None,
            "hugging_face_id": hugging_face_id,
            "benchmarks": {
                "artificial_analysis": {
                    "intelligence_index": 42.1,
                    "coding_index": 38.0,
                    "agentic_index": None,
                },
            },
        },
    )


class CatalogStub:
    def __init__(self, batches: list[tuple[OpenRouterModel, ...]]) -> None:
        self._batches = batches

    def fetch_catalog(
        self,
        url: str,
        *,
        retry_policy: FetchRetryPolicy,
        is_shutdown_requested: Callable[[], bool],
    ) -> tuple[OpenRouterModel, ...]:
        del url, retry_policy, is_shutdown_requested
        return self._batches.pop(0)


def make_monitor(
    catalog: CatalogStub,
    store: DeliveryStore,
    sender: RecordingSender,
) -> OpenRouterPriceMonitor:
    return OpenRouterPriceMonitor(
        FeedConfig(
            id="openrouter-prices",
            name="OpenRouter Price Drops",
            url=OPENROUTER_MODELS_URL,
            webhook="https://discord.test/webhooks/id/token",
            strategy="openrouter_prices",
            price_check_interval=3600,
            price_min_change_percentage=10,
            price_ignore_increases=True,
        ),
        PriceMonitorDependencies(
            catalog=catalog,
            snapshots=store,
            sender=sender,
            fetch_retry_policy=FetchRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            sqlite_retry_policy=SQLiteRetryPolicy(
                sleep=lambda seconds: True,
                on_retry=lambda error, delay: None,
            ),
            delivery=PriceAlertDelivery(
                sleep=lambda seconds: True,
                delay_between_posts=0,
                is_shutdown_requested=lambda: False,
            ),
        ),
    )


def test_price_monitor_seeds_only_eligible_paid_models(tmp_path: Path) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    (
                        make_model(),
                        make_model(
                            model_id="vendor/free",
                            name="Vendor: Free",
                            prompt="0",
                            completion="0",
                            cache="0",
                        ),
                        make_model(
                            model_id="~vendor/latest",
                            name="Vendor: Latest",
                        ),
                        make_model(
                            model_id="vendor/no-tools",
                            name="Vendor: No Tools",
                            tools=False,
                        ),
                    ),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()

        assert sender.messages == []
        snapshots = store.load_price_snapshots("openrouter-prices")
        assert len(snapshots) == 1
        assert snapshots[0].product_id == "vendor/model"
        assert snapshots[0].amount == Decimal(1)
        assert snapshots[0].formatted == "$1 / 1M"
        assert snapshots[0].currency == "USD"
        assert snapshots[0].available is None


def test_price_monitor_alerts_on_ten_percent_drop(tmp_path: Path) -> None:
    sender = RecordingSender([DiscordDeliveryResult.DELIVERED])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    (make_model(prompt="0.000001"),),
                    (make_model(prompt="0.00000089"),),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()

        assert len(sender.messages) == 1
        message = sender.messages[0]
        assert message.entry.title == "Vendor: Model"
        assert message.entry.price_direction is PriceDirection.DECREASE
        assert message.entry.price_change_percentage == "-11.0%"
        assert message.entry.source_metrics[0].value == "$0.89 / 1M"
        assert message.entry.source_metrics[1].value == "$1 / 1M"
        assert "Benchmarks: Intelligence 42.1 · Coding 38" in (
            message.entry.description
        )
        assert "Weights: https://huggingface.co/vendor/model" in (
            message.entry.description
        )


def test_price_monitor_suppresses_small_drop_and_increase(tmp_path: Path) -> None:
    sender = RecordingSender([])
    with DeliveryStore(tmp_path / "state.db") as store:
        monitor = make_monitor(
            CatalogStub(
                [
                    (make_model(prompt="0.000001"),),
                    (make_model(prompt="0.00000095"),),
                    (make_model(prompt="0.0000015"),),
                ],
            ),
            store,
            sender,
        )

        monitor.scan()
        monitor.scan()
        monitor.scan()

        assert sender.messages == []
        snapshots = store.load_price_snapshots("openrouter-prices")
        assert snapshots[0].amount == Decimal("1.5")
