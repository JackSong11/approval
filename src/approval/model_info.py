"""Model metadata — context windows, thinking support, max output tokens,
and the OpenAI tool-schema converter.

All knowledge about specific model names lives here so the Agent class
stays model-agnostic.
"""

from __future__ import annotations

from approval.tools import ToolDef

# ─── Model context windows ──────────────────────────────────

MODEL_CONTEXT: dict[str, int] = {
    "claude-opus-4-6": 200000,
    "claude-sonnet-4-6": 200000,
    "claude-sonnet-4-20250514": 200000,
    "claude-haiku-4-5-20251001": 200000,
    "claude-opus-4-20250514": 200000,
    "gpt-4o": 128000,
    "gpt-4o-mini": 128000,
}

DEFAULT_CONTEXT_WINDOW = 200000
# 预留给系统提示 / 工具 schema / 输出的安全余量
CONTEXT_HEADROOM = 20000


def get_context_window(model: str) -> int:
    """返回模型上下文窗口大小（token）。未登记的模型按默认值。"""
    return MODEL_CONTEXT.get(model, DEFAULT_CONTEXT_WINDOW)


def get_effective_window(model: str) -> int:
    """扣除安全余量后的可用上下文窗口。"""
    return get_context_window(model) - CONTEXT_HEADROOM


# ─── Thinking support detection ─────────────────────────────


def model_supports_thinking(model: str) -> bool:
    m = model.lower()
    # Claude 3.x / 3.5 / 3.7 系列不支持 thinking
    if "claude-3-" in m or "3-5-" in m or "3-7-" in m:
        return False
    if "claude" in m and any(x in m for x in ("opus", "sonnet", "haiku")):
        return True
    return False


# 判断模型是否支持更高级的“自适应思考（Adaptive Thinking）”。
def model_supports_adaptive_thinking(model: str) -> bool:
    m = model.lower()
    return "opus-4-6" in m or "sonnet-4-6" in m


def get_max_output_tokens(model: str) -> int:
    m = model.lower()
    if "opus-4-6" in m:
        return 64000
    if "sonnet-4-6" in m:
        return 32000
    if any(x in m for x in ("opus-4", "sonnet-4", "haiku-4")):
        return 32000
    return 16384


def resolve_thinking_mode(model: str, thinking_enabled: bool) -> str:
    """根据模型能力解析 thinking 模式：disabled / enabled / adaptive。"""
    if not thinking_enabled or not model_supports_thinking(model):
        return "disabled"
    if model_supports_adaptive_thinking(model):
        return "adaptive"
    return "enabled"


# ─── Convert tools to OpenAI format ─────────────────────────


def to_openai_tools(tools: list[ToolDef]) -> list[dict]:
    """将内部 ToolDef 列表转换为 OpenAI function-tool schema。"""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]
