"""Grade one answer. Multiple choice, letter by candidate position (same rule as lmms-eval)."""

from __future__ import annotations

from dataclasses import dataclass

from .model import ModelResult
from .tasks import Item


@dataclass(frozen=True)
class Scored:
    item_id: str
    cluster_key: str
    model_id: str
    pred_letter: str | None
    answer_letter: str
    correct: bool
    parse_failed: bool
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_s: float
    cached: bool


def score(item: Item, result: ModelResult) -> Scored:
    return Scored(
        item_id=item.item_id,
        cluster_key=item.cluster_key,
        model_id=result.model_id,
        pred_letter=result.pred_letter,
        answer_letter=item.answer_letter,
        correct=result.pred_letter == item.answer_letter,
        parse_failed=result.pred_letter is None,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        cost_usd=result.cost_usd,
        latency_s=result.latency_s,
        cached=result.cached,
    )
