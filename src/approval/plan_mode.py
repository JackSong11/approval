"""Plan mode helpers — plan file path generation and the system-prompt
suffix injected while the Agent is in plan mode.

The Agent class owns the *state machine* (entering/exiting plan mode,
approval flow). This module only provides the pure helpers so that prompt
text and filesystem layout stay out of agent.py.
"""

from __future__ import annotations

from pathlib import Path


def generate_plan_file_path(session_id: str) -> str:
    """为给定 session 在 ~/.claude/plans 下生成 plan 文件路径。"""
    d = Path.home() / ".claude" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    return str(d / f"plan-{session_id}.md")


def build_plan_mode_prompt(plan_file_path: str) -> str:
    """Plan 模式下追加到 system prompt 末尾的说明文本。"""
    return f"""

# Plan Mode Active

Plan mode is active. You MUST NOT make any edits (except the plan file below), run non-readonly tools, or make any changes to the system.

## Plan File: {plan_file_path}
Write your plan incrementally to this file using write_file or edit_file. This is the ONLY file you are allowed to edit.

## Workflow
1. **Explore**: Read code to understand the task. Use read_file, list_files, grep_search.
2. **Design**: Design your implementation approach. Use the agent tool with type="plan" if the task is complex.
3. **Write Plan**: Write a structured plan to the plan file including:
   - **Context**: Why this change is needed
   - **Steps**: Implementation steps with critical file paths
   - **Verification**: How to test the changes
4. **Exit**: Call exit_plan_mode when your plan is ready for user review.

IMPORTANT: When your plan is complete, you MUST call exit_plan_mode. Do NOT ask the user to approve — exit_plan_mode handles that."""


def resolve_target_mode_after_approval(choice: str, pre_plan_mode: str | None) -> str:
    """根据用户审批选择决定退出 plan 模式后的目标 permission_mode。"""
    if choice in ("clear-and-execute", "execute"):
        return "acceptEdits"
    # manual-execute 或其它未知值 → 恢复进入 plan 模式之前的状态
    return pre_plan_mode or "default"