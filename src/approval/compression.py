"""Multi-tier context compression for OpenAI-style message history.

The Agent owns the message list; this module provides pure(-ish) helpers
that mutate it in-place to reduce token pressure while preserving as much
useful detail as possible.

Three tiers, applied in order:
  1. budget_tool_results — truncate any single oversized tool result.
  2. snip_stale_results  — drop the *body* of older results from re-readable
                           tools (read_file, grep_search, ...).
  3. microcompact        — after a long idle, clear old tool results entirely.

Plus a separate `persist_large_result` helper used at write-time to swap a
huge tool output for a short preview + on-disk file path.
"""

from __future__ import annotations

import time
from pathlib import Path

# ─── 多层压缩常数 ────────────────────────
# ─── Multi-tier compression constants ────────────────────────


# 这些工具的输出可被随时重新获取，因此可以激进 snip
SNIPPABLE_TOOLS: set[str] = {"read_file", "grep_search", "list_files", "run_shell"}

SNIP_PLACEHOLDER = "[Content snipped - re-read if needed]"
STALE_PLACEHOLDER = "[Old result cleared]"

SNIP_THRESHOLD = 0.60  # 上下文利用率超过该比例时开始 snip
MICROCOMPACT_IDLE_S = 5 * 60  # 空闲超过 5 分钟触发 microcompact
KEEP_RECENT_RESULTS = 3  # 始终保留最近 N 个工具结果原文

LARGE_RESULT_THRESHOLD_BYTES = 30 * 1024  # 单个工具结果超过 30KB 写盘（txt文件，4万字符大概100KB。所以30KB纯英文是3万字左右，纯中文是1万字左右）
LARGE_RESULT_PREVIEW_LINES = 200


# ─── Tier 1: Budget tool results ─────────────────────────────


def budget_tool_results(messages: list[dict], last_input_tokens: int, effective_window: int) -> None:
    """利用率较高时，对单条过长的工具结果做头尾保留式截断。"""
    # 用于动态压缩（截断）发送给 OpenAI API 的工具返回结果（Tool Results）。其目的是在上下文窗口（Context Window）压力较大时，通过牺牲部分中间细节来节省 Token，防止超出模型限制。
    if not effective_window:
        return
    utilization = last_input_tokens / effective_window
    if utilization < 0.5:
        return
    budget = 15000 if utilization > 0.7 else 30000
    for msg in messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= budget:
            continue
        keep = (budget - 80) // 2
        truncated = len(content) - keep * 2
        msg["content"] = (
                content[:keep]
                + f"\n\n[... budgeted: {truncated} chars truncated ...]\n\n"
                + content[-keep:]
        )


# ─── Tier 2: Snip stale results ──────────────────────────────


def _find_tool_use_by_id(messages: list[dict], tool_use_id: str) -> dict | None:
    """根据 tool_call_id 反查 assistant 消息中的工具调用信息。"""
    if not tool_use_id:
        return None
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tool_calls = msg.get("tool_calls")
        if not tool_calls or not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if tool_call.get("id") == tool_use_id:
                function_info = tool_call.get("function", {}) or {}
                return {
                    "name": function_info.get("name"),
                    "input": function_info.get("arguments"),  # OpenAI 这里是 JSON 字符串
                }
    return None


def snip_stale_results(messages: list[dict], last_input_tokens: int, effective_window: int) -> None:
    """利用率高于阈值时，把较旧的可重读工具结果替换为占位符。"""
    if not effective_window:
        return
    utilization = last_input_tokens / effective_window
    if utilization < SNIP_THRESHOLD:
        return

    tool_msgs: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or content == SNIP_PLACEHOLDER:
            continue
        info = _find_tool_use_by_id(messages, msg.get("tool_call_id"))
        # 只 snip 那些可以重新拉取数据的工具
        # 关键逻辑：只有在 SNIPPABLE_TOOLS 列表中的工具才被加入待清理队列
        if info and info.get("name") in SNIPPABLE_TOOLS:
            tool_msgs.append(i)

    if len(tool_msgs) <= KEEP_RECENT_RESULTS:
        return
    snip_count = len(tool_msgs) - KEEP_RECENT_RESULTS
    for i in range(snip_count):
        messages[tool_msgs[i]]["content"] = SNIP_PLACEHOLDER


# ─── Tier 3: Microcompact ────────────────────────────────────


def microcompact(messages: list[dict], last_api_call_time: float) -> None:
    """长时间空闲后，把较旧的工具结果整体清空（更激进）。"""
    if not last_api_call_time or (time.time() - last_api_call_time) < MICROCOMPACT_IDLE_S:
        return
    tool_msgs: list[int] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content not in (SNIP_PLACEHOLDER, STALE_PLACEHOLDER):
            tool_msgs.append(i)
    clear_count = len(tool_msgs) - KEEP_RECENT_RESULTS
    for i in range(max(0, clear_count)):
        messages[tool_msgs[i]]["content"] = STALE_PLACEHOLDER


# ─── Pipeline entry point ────────────────────────────────────


def run_compression_pipeline(
        messages: list[dict],
        last_input_tokens: int,
        effective_window: int,
        last_api_call_time: float,
) -> None:
    """按顺序执行三层压缩。"""
    budget_tool_results(messages, last_input_tokens, effective_window)
    snip_stale_results(messages, last_input_tokens, effective_window)
    microcompact(messages, last_api_call_time)


# ─── Large result persistence ─────────────────────────────────
# When a tool result exceeds 30 KB, write it to disk and replace the
# context entry with a short preview + file path.  The model can use
# read_file to retrieve the full output later — no information is lost.
# ─── 大型结果持久化 ───────────────────────────────────
# 当工具结果超过 30 KB 时，将其写入磁盘并替换
# 上下文条目，替换为简短预览（200行） + 文件路径。模型稍后可以使用
# read_file 函数检索完整输出——不会丢失任何信息。


def persist_large_result(tool_name: str, result: str) -> str:
    """单条结果超过阈值则写盘，仅保留预览 + 路径。"""
    if len(result.encode()) <= LARGE_RESULT_THRESHOLD_BYTES:
        return result

    d = Path.home() / "tool-results"
    d.mkdir(parents=True, exist_ok=True)
    filename = f"{int(time.time() * 1000)}-{tool_name}.txt"
    filepath = d / filename
    filepath.write_text(result, encoding="utf-8")

    lines = result.split("\n")
    preview = "\n".join(lines[:LARGE_RESULT_PREVIEW_LINES])
    size_kb = len(result.encode()) / 1024

    return (
        f"[Result too large ({size_kb:.1f} KB, {len(lines)} lines). "
        f"Full output saved to {filepath}. "
        f"You can use read_file to see the full result.]\n\n"
        f"Preview (first {LARGE_RESULT_PREVIEW_LINES} lines):\n{preview}"
    )
