"""OpenRouter model catalog provider behavior."""

import json
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from rss2discord.configuration import FeedConfig
from rss2discord.fetch_errors import FeedFetchError
from rss2discord.providers.openrouter_models.bounds import OPENROUTER_MODELS_URL
from rss2discord.providers.openrouter_models.client import OpenRouterModelsClient
from rss2discord.providers.openrouter_models.models import OpenRouterModel
from rss2discord.providers.openrouter_models.strategy import (
    OpenRouterModelsStrategy,
)


def catalog_model(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "id": "openai/gpt-6.2-astra-pro",
        "canonical_slug": "openai/gpt-6.2-astra-pro-20261117",
        "name": "OpenAI: GPT-6.2 Astra Pro",
        "created": 1_794_938_432,
        "description": "A frontier model for <b>agents</b> &amp; tools.",
        "context_length": 1_048_576,
        "architecture": {
            "modality": "text+image->text",
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
            "tokenizer": "Other",
            "instruct_type": None,
        },
        "pricing": {
            "prompt": "0.00000125",
            "completion": "0.00001",
            "input_cache_read": "0.000000125",
        },
        "top_provider": {
            "context_length": 1_048_576,
            "max_completion_tokens": 65_536,
            "is_moderated": False,
        },
        "supported_parameters": ["tools", "tool_choice", "reasoning"],
        "expiration_date": None,
    }
    entry.update(overrides)
    return entry


def payload(*models: dict[str, object]) -> dict[str, object]:
    return {"data": list(models), "total_count": len(models)}


def client_with(data: dict[str, object]) -> OpenRouterModelsClient:
    return OpenRouterModelsClient(fetch=lambda _: json.dumps(data).encode())


def parse_model(**overrides: object) -> OpenRouterModel:
    return OpenRouterModel.model_validate(catalog_model(**overrides))


def test_client_parses_model_catalog() -> None:
    result = client_with(payload(catalog_model())).fetch_models(
        OPENROUTER_MODELS_URL,
    )

    assert len(result) == 1
    model = result[0]
    assert model.id == "openai/gpt-6.2-astra-pro"
    assert model.name == "OpenAI: GPT-6.2 Astra Pro"
    assert model.created == 1_794_938_432
    assert model.pricing.prompt == Decimal("0.00000125")
    assert model.pricing.completion == Decimal("0.00001")
    assert model.architecture is not None
    assert model.architecture.modality == "text+image->text"
    assert model.top_provider is not None
    assert model.top_provider.max_completion_tokens == 65_536
    assert "tools" in model.supported_parameters


@pytest.mark.parametrize(
    "url",
    [
        "http://openrouter.ai/api/v1/models",
        "https://example.com/api/v1/models",
        "https://openrouter.ai/api/v1/other",
        "https://openrouter.ai/api/v1/models?category=programming",
        "https://openrouter.ai/api/v1/models#fragment",
        "https://user@openrouter.ai/api/v1/models",
    ],
)
def test_client_rejects_invalid_urls(url: str) -> None:
    with pytest.raises(FeedFetchError) as error:
        client_with(payload(catalog_model())).fetch_models(url)

    assert error.value.cause_type == "InvalidUrl"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"data": []},
        {"data": "nope"},
        {"data": [{"id": 1}]},
        {"data": [{"id": "vendor/model", "pricing": "nope"}]},
    ],
)
def test_client_rejects_invalid_responses(data: dict[str, object]) -> None:
    with pytest.raises(FeedFetchError) as error:
        client_with(data).fetch_models(OPENROUTER_MODELS_URL)

    assert error.value.cause_type == "InvalidResponse"


def test_strategy_keeps_tool_capable_interactive_models() -> None:
    strategy = OpenRouterModelsStrategy(
        client=client_with(
            payload(
                catalog_model(),
                catalog_model(
                    id="inclusionai/ling-3.2-flash:free",
                    name="inclusionAI: Ling 3.2 Flash (free)",
                    pricing={"prompt": "0", "completion": "0"},
                ),
                catalog_model(
                    id="google/gemini-4-neon",
                    name="Google: Gemini 4 Neon",
                    supported_parameters=["reasoning"],
                ),
                catalog_model(
                    id="openai/gpt-6.2-astra-pro:batch",
                    name="OpenAI: GPT-6.2 Astra Pro (batch)",
                ),
                catalog_model(
                    id="openrouter/auto",
                    name="OpenRouter: Auto",
                    pricing={"prompt": "-1", "completion": "-1"},
                ),
                catalog_model(id="", name="Broken"),
            ),
        ),
    )

    entries, title = strategy.fetch_entries(OPENROUTER_MODELS_URL)

    assert title == "OpenRouter Models"
    assert [entry.id for entry in entries] == [
        "openai/gpt-6.2-astra-pro",
        "inclusionai/ling-3.2-flash:free",
    ]


def test_entry_id_changes_with_price_and_deprecation() -> None:
    strategy = OpenRouterModelsStrategy()
    base = parse_model()
    same = parse_model()
    cheaper = parse_model(
        pricing={
            "prompt": "0.000001",
            "completion": "0.00001",
            "input_cache_read": "0.000000125",
        },
    )
    cached = parse_model(
        pricing={
            "prompt": "0.00000125",
            "completion": "0.00001",
            "input_cache_read": "0.0000001",
        },
    )
    deprecating = parse_model(expiration_date="2026-12-11")

    base_id = strategy.get_entry_id(base)
    assert strategy.get_entry_id(same) == base_id
    assert strategy.get_entry_id(cheaper) != base_id
    assert strategy.get_entry_id(cached) != base_id
    assert strategy.get_entry_id(deprecating) != base_id


def test_entry_data_contains_pricing_metrics() -> None:
    strategy = OpenRouterModelsStrategy()
    released = datetime.fromtimestamp(1_794_938_432, UTC).strftime("%Y-%m-%d")

    data = strategy.get_entry_data(parse_model())

    assert data.title == "OpenAI: GPT-6.2 Astra Pro"
    assert data.link == "https://openrouter.ai/openai/gpt-6.2-astra-pro"
    assert data.author == "OpenAI"
    assert data.categories == ("model",)
    assert "A frontier model for agents & tools." in data.description
    assert "Model ID: `openai/gpt-6.2-astra-pro`" in data.description
    assert [metric.label for metric in data.source_metrics] == [
        "Input",
        "Output",
        "Cache read",
        "Context",
        "Max output",
        "Modalities",
        "Released",
    ]
    assert data.source_metrics[0].value == "$1.25 / 1M"
    assert data.source_metrics[1].value == "$10 / 1M"
    assert data.source_metrics[2].value == "$0.125 / 1M"
    assert data.source_metrics[3].value == "1M tokens"
    assert data.source_metrics[4].value == "65.5K tokens"
    assert data.source_metrics[5].value == "text + image → text"
    assert data.source_metrics[6].value == released


def test_free_model_prices_are_labelled_free() -> None:
    strategy = OpenRouterModelsStrategy()
    entry = parse_model(
        id="inclusionai/ling-3.2-flash:free",
        name="inclusionAI: Ling 3.2 Flash (free)",
        pricing={"prompt": "0", "completion": "0"},
    )

    data = strategy.get_entry_data(entry)

    assert data.source_metrics[0].value == "Free"
    assert data.source_metrics[1].value == "Free"
    assert strategy.get_entry_id(entry) == (
        "inclusionai/ling-3.2-flash:free:free:free:na:none"
    )


def test_variable_price_is_labelled_variable() -> None:
    strategy = OpenRouterModelsStrategy()
    entry = parse_model(
        id="openrouter/fusion",
        name="OpenRouter: Fusion",
        pricing={"prompt": "-1", "completion": "-1"},
    )

    data = strategy.get_entry_data(entry)

    assert data.source_metrics[0].value == "Variable"
    assert data.source_metrics[1].value == "Variable"
    assert "variable" in strategy.get_entry_id(entry)


def test_entry_data_tolerates_missing_optional_fields() -> None:
    strategy = OpenRouterModelsStrategy()
    entry = parse_model(
        created=None,
        description=None,
        context_length=None,
        architecture=None,
        pricing={},
        top_provider=None,
        expiration_date=None,
    )

    data = strategy.get_entry_data(entry)

    assert [metric.label for metric in data.source_metrics] == [
        "Input",
        "Output",
    ]
    assert [metric.value for metric in data.source_metrics] == [
        "Unknown",
        "Unknown",
    ]
    assert data.description == "Model ID: `openai/gpt-6.2-astra-pro`"
    assert strategy.get_entry_id(entry).endswith(":na:na:na:none")


def test_config_accepts_openrouter_models_strategy() -> None:
    feed = FeedConfig(
        id="openrouter-models",
        url=OPENROUTER_MODELS_URL,
        webhook="https://discord.test/webhook",
        strategy="openrouter_models",
    )

    assert feed.strategy == "openrouter_models"
