"""OpenRouter model catalog response models."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _OpenRouterModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class OpenRouterArchitecture(_OpenRouterModel):
    modality: str | None = None
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    tokenizer: str | None = None


class OpenRouterPricing(_OpenRouterModel):
    """Per-token prices in USD. Negative values mean variable pricing."""

    prompt: Decimal | None = None
    completion: Decimal | None = None
    input_cache_read: Decimal | None = None


class OpenRouterTopProvider(_OpenRouterModel):
    context_length: int | None = None
    max_completion_tokens: int | None = None


class OpenRouterArtificialAnalysis(_OpenRouterModel):
    intelligence_index: float | None = None
    coding_index: float | None = None
    agentic_index: float | None = None


class OpenRouterBenchmarks(_OpenRouterModel):
    artificial_analysis: OpenRouterArtificialAnalysis | None = None


class OpenRouterModel(_OpenRouterModel):
    id: str = ""
    canonical_slug: str | None = None
    name: str = ""
    created: int | None = None
    description: str | None = None
    context_length: int | None = None
    architecture: OpenRouterArchitecture | None = None
    pricing: OpenRouterPricing = Field(default_factory=OpenRouterPricing)
    top_provider: OpenRouterTopProvider | None = None
    supported_parameters: tuple[str, ...] = ()
    expiration_date: str | None = None
    hugging_face_id: str | None = None
    benchmarks: OpenRouterBenchmarks | None = None


class OpenRouterModelsResponse(_OpenRouterModel):
    data: tuple[OpenRouterModel, ...]
