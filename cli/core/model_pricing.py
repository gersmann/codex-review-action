from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .review_usage import ReviewUsage


@dataclass(frozen=True)
class ModelPricing:
    input_per_million: Decimal
    cached_input_per_million: Decimal
    output_per_million: Decimal


MODEL_PRICING = {
    "gpt-5.6-luna": ModelPricing(
        input_per_million=Decimal("1.00"),
        cached_input_per_million=Decimal("0.10"),
        output_per_million=Decimal("6.00"),
    ),
    "gpt-5.6-terra": ModelPricing(
        input_per_million=Decimal("2.50"),
        cached_input_per_million=Decimal("0.25"),
        output_per_million=Decimal("15.00"),
    ),
    "gpt-5.6-sol": ModelPricing(
        input_per_million=Decimal("5.00"),
        cached_input_per_million=Decimal("0.50"),
        output_per_million=Decimal("30.00"),
    ),
}

SUPPORTED_REVIEW_MODELS = tuple(MODEL_PRICING)


def estimate_review_cost(model_name: str, usage: ReviewUsage) -> Decimal:
    pricing = MODEL_PRICING[model_name]
    cached_input_tokens = min(usage.cached_input_tokens, usage.input_tokens)
    uncached_input_tokens = usage.input_tokens - cached_input_tokens
    cost = (
        Decimal(uncached_input_tokens) * pricing.input_per_million
        + Decimal(cached_input_tokens) * pricing.cached_input_per_million
        + Decimal(usage.output_tokens) * pricing.output_per_million
    )
    return cost / Decimal(1_000_000)
