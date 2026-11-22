"""OpenRouter model-release discovery strategy."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import final, override

from rss2discord.models import EntryData, EntryId, SourceMetric
from rss2discord.providers.openrouter_models.bounds import OPENROUTER_LABEL
from rss2discord.providers.openrouter_models.client import OpenRouterModelsClient
from rss2discord.providers.openrouter_models.models import OpenRouterModel
from rss2discord.transports.base import ScraperStrategy

OPENROUTER_SOURCE_TITLE = "OpenRouter Models"
OPENROUTER_MODEL_LINK = "https://openrouter.ai/"
_TOOL_PARAMETER = "tools"
_BATCH_SUFFIX = ":batch"
_ROUTER_PREFIX = "openrouter/"
_PRICE_QUANTUM = Decimal("0.0001")
_PRICE_UNIT = Decimal(1_000_000)
_DESCRIPTION_LIMIT = 900


@final
class OpenRouterModelsStrategy(ScraperStrategy):
    """Discover tool-capable OpenRouter models and their current prices."""

    seed_existing_on_first_fetch = True

    def __init__(self, client: OpenRouterModelsClient | None = None) -> None:
        self._client = client or OpenRouterModelsClient()

    @override
    def fetch_entries(self, url: str) -> tuple[list[OpenRouterModel], str]:
        models = self._client.fetch_models(url)
        return (
            [model for model in models if _is_model_entry(model)],
            OPENROUTER_SOURCE_TITLE,
        )

    @override
    def get_entry_id(self, entry: OpenRouterModel) -> EntryId:
        # A new price or deprecation date reposts the model.
        return EntryId(
            ":".join(
                (
                    entry.id,
                    _price_key(entry.pricing.prompt),
                    _price_key(entry.pricing.completion),
                    _price_key(entry.pricing.input_cache_read),
                    entry.expiration_date or "none",
                ),
            ),
        )

    @override
    def get_entry_data(self, entry: OpenRouterModel) -> EntryData:
        pricing = entry.pricing
        metrics = [
            SourceMetric("Input", _price_text(pricing.prompt)),
            SourceMetric("Output", _price_text(pricing.completion)),
        ]
        if pricing.input_cache_read is not None:
            metrics.append(
                SourceMetric("Cache read", _price_text(pricing.input_cache_read)),
            )
        if entry.context_length is not None and entry.context_length > 0:
            metrics.append(
                SourceMetric("Context", _format_tokens(entry.context_length)),
            )
        if (
            entry.top_provider is not None
            and entry.top_provider.max_completion_tokens is not None
            and entry.top_provider.max_completion_tokens > 0
        ):
            metrics.append(
                SourceMetric(
                    "Max output",
                    _format_tokens(entry.top_provider.max_completion_tokens),
                ),
            )
        if entry.architecture is not None and entry.architecture.modality:
            metrics.append(
                SourceMetric(
                    "Modalities",
                    _format_modality(entry.architecture.modality),
                ),
            )
        if entry.created is not None:
            released = _format_date(entry.created)
            if released is not None:
                metrics.append(SourceMetric("Released", released))
        if entry.expiration_date:
            metrics.append(SourceMetric("Deprecating", entry.expiration_date))

        description_lines = []
        summary = self._truncate(
            self._clean_html(entry.description or ""),
            _DESCRIPTION_LIMIT,
        )
        if summary:
            description_lines.append(summary)
        description_lines.append(f"Model ID: `{entry.id}`")

        return EntryData(
            title=entry.name.strip() or entry.id,
            link=f"{OPENROUTER_MODEL_LINK}{entry.id}",
            description="\n".join(description_lines),
            author=_vendor(entry),
            timestamp=datetime.now(UTC).isoformat(),
            categories=("model",),
            source_metrics=tuple(metrics),
        )


def _is_model_entry(entry: OpenRouterModel) -> bool:
    """Keep interactive tool-capable models, not batch endpoints or routers."""
    return bool(
        entry.id.strip()
        and entry.name.strip()
        and _TOOL_PARAMETER in entry.supported_parameters
        and not entry.id.endswith(_BATCH_SUFFIX)
        and not entry.id.startswith(_ROUTER_PREFIX),
    )


def _price_key(price: Decimal | None) -> str:
    if price is None:
        return "na"
    if price < 0:
        return "variable"
    if price.is_zero():
        return "free"
    return _per_million(price)


def _price_text(price: Decimal | None) -> str:
    if price is None:
        return "Unknown"
    if price < 0:
        return "Variable"
    if price.is_zero():
        return "Free"
    return f"${_per_million(price)} / 1M"


def _per_million(price: Decimal) -> str:
    amount = (price * _PRICE_UNIT).quantize(_PRICE_QUANTUM)
    return format(amount.normalize(), "f")


def _format_tokens(value: int) -> str:
    if value >= 1_000_000:
        millions = f"{value / 1_000_000:.1f}".removesuffix(".0")
        return f"{millions}M tokens"
    if value >= 1_000:
        thousands = f"{value / 1_000:.1f}".removesuffix(".0")
        return f"{thousands}K tokens"
    return f"{value} tokens"


def _format_modality(modality: str) -> str:
    return modality.replace("+", " + ").replace("->", " → ")


def _format_date(epoch: int) -> str | None:
    try:
        moment = datetime.fromtimestamp(epoch, UTC)
    except (OSError, OverflowError, ValueError):
        return None
    return moment.strftime("%Y-%m-%d")


def _vendor(entry: OpenRouterModel) -> str:
    prefix, separator, _ = entry.name.partition(": ")
    if separator and prefix.strip():
        return prefix.strip()
    vendor, separator, _ = entry.id.partition("/")
    if separator and vendor:
        return vendor
    return OPENROUTER_LABEL
