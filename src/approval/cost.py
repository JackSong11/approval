"""Token-based USD cost estimation and budget checks.

Pricing constants are placed here so they're easy to update. Currently uses
a single rate (Claude Sonnet/Opus tier) for simplicity; extend if multi-rate
billing is needed.
"""

from __future__ import annotations

from typing import TypedDict

# USD per 1M tokens
INPUT_TOKEN_PRICE_PER_M = 3.0
OUTPUT_TOKEN_PRICE_PER_M = 15.0


class BudgetStatus(TypedDict):
    exceeded: bool
    reason: str


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> float:
    """根据累计 token 数估算美元费用。"""
    return (
        (input_tokens / 1_000_000) * INPUT_TOKEN_PRICE_PER_M
        + (output_tokens / 1_000_000) * OUTPUT_TOKEN_PRICE_PER_M
    )


def check_budget(
    input_tokens: int,
    output_tokens: int,
    current_turns: int,
    max_cost_usd: float | None,
    max_turns: int | None,
) -> BudgetStatus:
    """检查是否触达成本或轮数预算上限。"""
    if max_cost_usd is not None:
        cost = estimate_cost_usd(input_tokens, output_tokens)
        if cost >= max_cost_usd:
            return {
                "exceeded": True,
                "reason": f"Cost limit reached (${cost:.4f} >= ${max_cost_usd})",
            }
    if max_turns is not None and current_turns >= max_turns:
        return {
            "exceeded": True,
            "reason": f"Turn limit reached ({current_turns} >= {max_turns})",
        }
    return {"exceeded": False, "reason": ""}