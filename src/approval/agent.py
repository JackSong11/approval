"""Agent core loop — dual backend (OpenAI compatible), streaming,
4-layer compression, plan mode, sub-agents, budget control.
Mirrors Approval's agent architecture."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import Awaitable, Callable

from langfuse import observe, propagate_attributes
from langfuse.openai import openai  # OpenAI integration

from approval.tools import (
    tool_definitions,
    execute_tool,
    check_permission,
    CONCURRENCY_SAFE_TOOLS,
    get_active_tool_definitions,
    ToolDef,
)
from approval.memory import (
    start_memory_prefetch,
    format_memories_for_injection,
    MemoryPrefetch,
)
from approval.ui import (
    print_assistant_text,
    print_tool_call,
    print_tool_result,
    print_confirmation,
    print_divider,
    print_cost,
    print_info,
    print_sub_agent_start,
    print_sub_agent_end,
    start_spinner,
    stop_spinner,
)
from approval.session import save_session
from approval.prompt import build_system_prompt
from approval.subagent import get_sub_agent_config
from approval.mcp_client import McpManager

from approval.retry import with_retry
from approval.model_info import (
    get_effective_window,
    resolve_thinking_mode,
    to_openai_tools,
)
from approval.compression import (
    persist_large_result,
    run_compression_pipeline,
)
from approval.cost import check_budget, estimate_cost_usd
from approval.plan_mode import (
    build_plan_mode_prompt,
    generate_plan_file_path,
    resolve_target_mode_after_approval,
)


# ─── Agent ───────────────────────────────────────────────────


class Agent:
    # self后面紧跟着一个单独的 * 符号，这被称为 “强制关键字参数”（Keyword - OnlyArguments）。强制调用者必须通过“键=值”的形式来传递后续的所有参数。
    def __init__(
        self,
        *,
        permission_mode: str = "default",
        model: str = "claude-opus-4-6",
        api_base: str | None = None,
        api_key: str | None = None,
        thinking: bool = False,
        max_cost_usd: float | None = None,
        max_turns: int | None = None,
        confirm_fn: Callable[[str], Awaitable[bool]] | None = None,
        custom_system_prompt: str | None = None,
        custom_tools: list[ToolDef] | None = None,
        is_sub_agent: bool = False,
    ):
        # ── 配置 ─────────────────────────────────────────────
        self.permission_mode = permission_mode
        self.thinking = thinking
        self.model = model
        self.is_sub_agent = is_sub_agent
        self.tools = custom_tools or tool_definitions
        self.max_cost_usd = max_cost_usd
        self.max_turns = max_turns
        self.confirm_fn = confirm_fn
        self.effective_window = get_effective_window(model)

        # ── 会话标识 ─────────────────────────────────────────
        self.session_id = uuid.uuid4().hex[:8]
        self.session_start_time = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        # ── token / 轮数 / 计时统计 ──────────────────────────
        # 直接采用 API 返回的真实用量，比 Claude Code 的锚点+估算简单
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0  # 用于判断是否接近窗口上限
        self.current_turns = 0
        self.last_api_call_time = 0.0

        # ── 中断支持 ─────────────────────────────────────────
        self._aborted = False
        self._current_task: asyncio.Task | None = None

        # ── 权限白名单（同一路径只确认一次）─────────────────
        self._confirmed_paths: set[str] = set()

        # ── Plan 模式状态 ────────────────────────────────────
        self._pre_plan_mode: str | None = None   # 进入前的 permission_mode
        self._plan_file_path: str | None = None  # 当前 plan 文件路径
        self._plan_approval_fn: Callable[[str], Awaitable[dict]] | None = None
        self._context_cleared: bool = False      # plan 审批选择 clear-and-execute 时置 True

        # ── Thinking 模式 ────────────────────────────────────
        self._thinking_mode = resolve_thinking_mode(model, thinking)

        # ── 输出缓冲（子代理把输出收集起来返回给父代理）──────
        self._output_buffer: list[str] | None = None

        # ── 读后编辑保护：记录文件 mtime ─────────────────────
        self._read_file_state: dict[str, float] = {}

        # ── MCP 工具集成 ─────────────────────────────────────
        self._mcp_manager = McpManager()
        self._mcp_initialized = False

        # ── 记忆召回（每用户回合的语义 prefetch）─────────────
        self._already_surfaced_memories: set[str] = set()
        self._session_memory_bytes = 0

        # ── System prompt ────────────────────────────────────
        self._base_system_prompt = custom_system_prompt or build_system_prompt()
        if self.permission_mode == "plan":
            self._plan_file_path = generate_plan_file_path(self.session_id)
            self._system_prompt = self._base_system_prompt + build_plan_mode_prompt(self._plan_file_path)
        else:
            self._system_prompt = self._base_system_prompt

        # ── OpenAI 客户端 & 消息历史 ─────────────────────────
        self._openai_client = openai.AsyncOpenAI(base_url=api_base, api_key=api_key)
        self._openai_messages: list[dict] = [
            {"role": "system", "content": self._system_prompt}
        ]

    # ─── Public properties / setters ─────────────────────────

    @property
    def is_processing(self) -> bool:
        return self._current_task is not None and not self._current_task.done()

    def abort(self) -> None:
        self._aborted = True
        if self._current_task and not self._current_task.done():
            self._current_task.cancel()

    def set_confirm_fn(self, fn: Callable[[str], Awaitable[bool]]) -> None:
        self.confirm_fn = fn

    def set_plan_approval_fn(self, fn: Callable[[str], Awaitable[dict]]) -> None:
        self._plan_approval_fn = fn

    def get_token_usage(self) -> dict:
        return {"input": self.total_input_tokens, "output": self.total_output_tokens}

    # ─── Side-query (used by memory recall) ──────────────────

    def _build_side_query(self):
        """构建一个 side-query 可调用，供 memory recall 在后台使用同一模型做检索判断。"""
        client = self._openai_client
        if not client:
            return None
        model = self.model

        async def _side_query(system: str, user_message: str) -> str:
            resp = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_message},
                ],
            )
            return resp.choices[0].message.content or "" if resp.choices else ""

        return _side_query

    # ─── Plan mode toggle (REPL command) ─────────────────────

    def toggle_plan_mode(self) -> str:
        """REPL `/plan` 命令：在 plan 模式与原模式间切换。"""
        if self.permission_mode == "plan":
            self.permission_mode = self._pre_plan_mode or "default"
            self._pre_plan_mode = None
            self._plan_file_path = None
            self._system_prompt = self._base_system_prompt
            if self._openai_messages:
                self._openai_messages[0]["content"] = self._system_prompt
            print_info(f"Exited plan mode → {self.permission_mode} mode")
            return self.permission_mode

        self._pre_plan_mode = self.permission_mode
        self.permission_mode = "plan"
        self._plan_file_path = generate_plan_file_path(self.session_id)
        self._system_prompt = self._base_system_prompt + build_plan_mode_prompt(self._plan_file_path)
        if self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt
        print_info(f"Entered plan mode. Plan file: {self._plan_file_path}")
        return "plan"

    # ─── Main chat entry point ───────────────────────────────

    async def chat(self, user_message: str) -> None:
        # 首次聊天时（仅 main agent）懒加载 MCP 服务器
        if not self._mcp_initialized and not self.is_sub_agent:
            self._mcp_initialized = True
            try:
                await self._mcp_manager.load_and_connect()
                mcp_defs = self._mcp_manager.get_tool_definitions()
                if mcp_defs:
                    self.tools = self.tools + mcp_defs
            except Exception as e:
                print(f"[mcp] Init failed: {e}", flush=True)

        self._aborted = False
        coro = self._chat_openai(user_message)
        self._current_task = asyncio.current_task()
        try:
            await coro
        except asyncio.CancelledError:
            self._aborted = True
        finally:
            self._current_task = None

        if not self.is_sub_agent:
            print_divider()
            self._auto_save()

    # ─── Sub-agent entry point ───────────────────────────────

    async def run_once(self, prompt: str) -> dict:
        """子代理一次性运行：捕获文本输出和增量 token 用量。"""
        self._output_buffer = []
        prev_in = self.total_input_tokens
        prev_out = self.total_output_tokens
        await self.chat(prompt)
        text = "".join(self._output_buffer)
        self._output_buffer = None
        return {
            "text": text,
            "tokens": {
                "input": self.total_input_tokens - prev_in,
                "output": self.total_output_tokens - prev_out,
            },
        }

    # ─── Output helper ───────────────────────────────────────

    def _emit_text(self, text: str) -> None:
        if self._output_buffer is not None:
            self._output_buffer.append(text)
        else:
            print_assistant_text(text)

    # ─── REPL commands ───────────────────────────────────────

    def clear_history(self) -> None:
        self._openai_messages = []
        self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.last_input_token_count = 0
        print_info("Conversation cleared.")

    def show_cost(self) -> None:
        total = estimate_cost_usd(self.total_input_tokens, self.total_output_tokens)
        budget_info = f" / ${self.max_cost_usd} budget" if self.max_cost_usd else ""
        turn_info = f" | Turns: {self.current_turns}/{self.max_turns}" if self.max_turns else ""
        print_info(
            f"Tokens: {self.total_input_tokens} in / {self.total_output_tokens} out\n"
            f"  Estimated cost: ${total:.4f}{budget_info}{turn_info}"
        )

    async def compact(self) -> None:
        await self._compact_conversation()

    # ─── Session restore / save ──────────────────────────────

    def restore_session(self, data: dict) -> None:
        if data.get("openaiMessages"):
            self._openai_messages = data["openaiMessages"]
        print_info(f"Session restored ({self._get_message_count()} messages).")

    def _get_message_count(self) -> int:
        return len(self._openai_messages)

    def _auto_save(self) -> None:
        try:
            save_session(self.session_id, {
                "metadata": {
                    "id": self.session_id,
                    "model": self.model,
                    "cwd": str(Path.cwd()),
                    "startTime": self.session_start_time,
                    "messageCount": self._get_message_count(),
                },
                "openaiMessages": self._openai_messages,
            })
        except Exception:
            # 自动保存失败不影响主流程
            pass

    # ─── Autocompact (summary-based) ─────────────────────────

    async def _check_and_compact(self) -> None:
        if self.last_input_token_count > self.effective_window * 0.85:
            print_info("Context window filling up, compacting conversation...")
            await self._compact_conversation()

    async def _compact_conversation(self) -> None:
        if len(self._openai_messages) < 5:
            return
        system_msg = self._openai_messages[0]
        last_user_msg = self._openai_messages[-1]
        summary_resp = await self._openai_client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a conversation summarizer. Be concise but preserve important details.",
                },
                # 传入除 system 和最新提问之外的所有中间对话
                *self._openai_messages[1:-1],
                {
                    "role": "user",
                    "content": (
                        "Summarize the conversation so far in a concise paragraph, "
                        "preserving key decisions, file paths, and context needed to continue the work."
                    ),
                },
            ],
        )
        summary_text = summary_resp.choices[0].message.content or "No summary available."
        self._openai_messages = [
            system_msg,
            {"role": "user", "content": f"[Previous conversation summary]\n{summary_text}"},
            {
                "role": "assistant",
                "content": "Understood. I have the context from our previous conversation. How can I continue helping?",
            },
        ]
        if last_user_msg.get("role") == "user":
            self._openai_messages.append(last_user_msg)
        self.last_input_token_count = 0
        print_info("Conversation compacted.")

    # ─── Tool execution dispatch ─────────────────────────────

    async def _execute_tool_call(self, name: str, inp: dict) -> str:
        """根据工具名分发到 plan 模式 / 子代理 / skill / MCP / 内置工具。"""
        if name in ("enter_plan_mode", "exit_plan_mode"):
            return await self._execute_plan_mode_tool(name)
        if name == "agent":
            return await self._execute_agent_tool(inp)
        if name == "skill":
            return await self._execute_skill_tool(inp)
        # Route MCP tool calls to the MCP manager
        if self._mcp_manager.is_mcp_tool(name):
            return await self._mcp_manager.call_tool(name, inp)
        return await execute_tool(name, inp, self._read_file_state)

    # ─── Skill fork mode ─────────────────────────────────────

    async def _execute_skill_tool(self, inp: dict) -> str:
        from .skills import execute_skill

        skill_name = inp.get("skill_name", "")
        args = inp.get("args", "") or ""
        result = execute_skill(skill_name, args)
        if not result:
            return f"Unknown skill: {skill_name}"

        # 非 fork 模式：直接把 skill prompt 作为工具输出回写
        if result["context"] != "fork":
            return f'[Skill "{skill_name}" activated]\n\n{result["prompt"]}'

        # fork 模式：在子代理里执行 skill
        tools = (
            [t for t in self.tools if t["name"] in result["allowed_tools"]]
            if result.get("allowed_tools")
            # 不让子代理再 fork 子代理，避免无限递归
            else [t for t in self.tools if t["name"] != "agent"]
        )
        print_sub_agent_start("skill-fork", skill_name)
        sub_agent = Agent(
            model=self.model,
            api_base=str(self._openai_client.base_url) if self._openai_client else None,
            custom_system_prompt=result["prompt"],
            custom_tools=tools,
            is_sub_agent=True,
            permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
        )
        try:
            sub_result = await sub_agent.run_once(args or "Execute this skill task.")
            self.total_input_tokens += sub_result["tokens"]["input"]
            self.total_output_tokens += sub_result["tokens"]["output"]
            return sub_result["text"] or "(Skill produced no output)"
        except Exception as e:
            return f"Skill fork error: {e}"
        finally:
            print_sub_agent_end("skill-fork", skill_name)

    # ─── Plan mode tool implementations ──────────────────────

    def _enter_plan_mode(self) -> str:
        if self.permission_mode == "plan":
            return "Already in plan mode."
        self._pre_plan_mode = self.permission_mode
        self.permission_mode = "plan"
        self._plan_file_path = generate_plan_file_path(self.session_id)
        self._system_prompt = self._base_system_prompt + build_plan_mode_prompt(self._plan_file_path)
        if self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt
        print_info(f"Entered plan mode (read-only). Plan file: {self._plan_file_path}")
        return (
            f"Entered plan mode. You are now in read-only mode.\n\n"
            f"Your plan file: {self._plan_file_path}\n"
            f"Write your plan to this file. This is the only file you can edit.\n\n"
            f"When your plan is complete, call exit_plan_mode."
        )

    def _read_plan_content(self) -> str:
        if self._plan_file_path and Path(self._plan_file_path).exists():
            return Path(self._plan_file_path).read_text()
        return "(No plan file found)"

    def _restore_after_plan_exit(self, target_mode: str) -> None:
        """退出 plan 模式后恢复 system prompt / permission_mode 等。"""
        self.permission_mode = target_mode
        self._pre_plan_mode = None
        self._plan_file_path = None
        self._system_prompt = self._base_system_prompt
        if self._openai_messages:
            self._openai_messages[0]["content"] = self._system_prompt

    async def _execute_plan_mode_tool(self, name: str) -> str:
        if name == "enter_plan_mode":
            return self._enter_plan_mode()

        if name == "exit_plan_mode":
            if self.permission_mode != "plan":
                return "Not in plan mode."

            plan_content = self._read_plan_content()
            saved_plan_path = self._plan_file_path

            # 没有交互审批函数（如子代理）→ 直接退回原模式
            if not self._plan_approval_fn:
                fallback_mode = self._pre_plan_mode or "default"
                self._restore_after_plan_exit(fallback_mode)
                print_info(f"Exited plan mode. Restored to {self.permission_mode} mode.")
                return (
                    f"Exited plan mode. Permission mode restored to: {self.permission_mode}\n\n"
                    f"## Your Plan:\n{plan_content}"
                )

            # 交互审批
            result = await self._plan_approval_fn(plan_content)
            choice = result.get("choice", "manual-execute")

            if choice == "keep-planning":
                feedback = result.get("feedback") or "Please revise the plan."
                return (
                    f"User rejected the plan and wants to keep planning.\n\n"
                    f"User feedback: {feedback}\n\n"
                    f"Please revise your plan based on this feedback. When done, call exit_plan_mode again."
                )

            target_mode = resolve_target_mode_after_approval(choice, self._pre_plan_mode)
            self._restore_after_plan_exit(target_mode)

            if choice == "clear-and-execute":
                self._clear_history_keep_system()
                self._context_cleared = True
                print_info(f"Plan approved. Context cleared, executing in {target_mode} mode.")
                return (
                    f"User approved the plan. Context was cleared. Permission mode: {target_mode}\n\n"
                    f"Plan file: {saved_plan_path}\n\n"
                    f"## Approved Plan:\n{plan_content}\n\n"
                    f"Proceed with implementation."
                )

            print_info(f"Plan approved. Executing in {target_mode} mode.")
            return (
                f"User approved the plan. Permission mode: {target_mode}\n\n"
                f"## Approved Plan:\n{plan_content}\n\n"
                f"Proceed with implementation."
            )

        return f"Unknown plan mode tool: {name}"

    def _clear_history_keep_system(self) -> None:
        """清空对话但保留 system prompt（用于 clear-and-execute 审批路径）。"""
        self._openai_messages = []
        self._openai_messages.append({"role": "system", "content": self._system_prompt})
        self.last_input_token_count = 0

    # ─── Sub-agent tool ──────────────────────────────────────

    async def _execute_agent_tool(self, inp: dict) -> str:
        agent_type = inp.get("type", "general")
        description = inp.get("description", "sub-agent task")
        prompt = inp.get("prompt", "")

        print_sub_agent_start(agent_type, description)

        config = get_sub_agent_config(agent_type)
        sub_agent = Agent(
            model=self.model,
            api_base=str(self._openai_client.base_url) if self._openai_client else None,
            custom_system_prompt=config["system_prompt"],
            custom_tools=config["tools"],
            is_sub_agent=True,
            permission_mode="plan" if self.permission_mode == "plan" else "bypassPermissions",
        )

        try:
            result = await sub_agent.run_once(prompt)
            self.total_input_tokens += result["tokens"]["input"]
            self.total_output_tokens += result["tokens"]["output"]
            return result["text"] or "(Sub-agent produced no output)"
        except Exception as e:
            return f"Sub-agent error: {e}"
        finally:
            print_sub_agent_end(agent_type, description)

    # ─── Confirmation helper ─────────────────────────────────

    async def _confirm_dangerous(self, command: str) -> bool:
        print_confirmation(command)
        if self.confirm_fn:
            return await self.confirm_fn(command)
        # Fallback：阻塞式 input
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    # ─── Memory prefetch handling ────────────────────────────

    def _consume_memory_prefetch(self, memory_prefetch: MemoryPrefetch) -> None:
        """如果记忆 prefetch 已完成且未消费，把召回结果注入到最近一条 user 消息。"""
        if memory_prefetch.consumed or not memory_prefetch.settled:
            return
        memory_prefetch.consumed = True
        try:
            memories = memory_prefetch.task.result()
        except Exception:
            # prefetch 内部错误已在 memory 模块打印
            return
        if not memories:
            return

        injection_text = format_memories_for_injection(memories)
        last = self._openai_messages[-1] if self._openai_messages else None
        if last and last.get("role") == "user":
            last["content"] = (last.get("content") or "") + "\n\n" + injection_text
        else:
            self._openai_messages.append({"role": "user", "content": injection_text})

        for m in memories:
            self._already_surfaced_memories.add(m.path)
            self._session_memory_bytes += len(m.content.encode())

    # ─── Tool-call permission pre-check ──────────────────────

    async def _check_tool_calls(self, tool_calls: list[dict]) -> list[dict]:
        """对一批 tool_calls 串行做权限/确认检查，返回包含 allowed/result 的条目列表。"""
        checked: list[dict] = []
        for tc in tool_calls:
            if self._aborted:
                break
            if tc.get("type") != "function":
                continue
            fn_name = tc["function"]["name"]
            try:
                inp = json.loads(tc["function"]["arguments"])
            except Exception:
                inp = {}

            print_tool_call(fn_name, inp)

            perm = check_permission(fn_name, inp, self.permission_mode, self._plan_file_path)
            if perm["action"] == "deny":
                print_info(f"Denied: {perm.get('message', '')}")
                checked.append({
                    "tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                    "result": f"Action denied: {perm.get('message', '')}",
                })
                continue

            if (
                perm["action"] == "confirm"
                and perm.get("message")
                and perm["message"] not in self._confirmed_paths
            ):
                confirmed = await self._confirm_dangerous(perm["message"])
                if not confirmed:
                    checked.append({
                        "tc": tc, "fn": fn_name, "inp": inp, "allowed": False,
                        "result": "User denied this action.",
                    })
                    continue
                self._confirmed_paths.add(perm["message"])

            checked.append({"tc": tc, "fn": fn_name, "inp": inp, "allowed": True})
        return checked

    @staticmethod
    def _group_into_batches(checked: list[dict]) -> list[dict]:
        """把连续的安全工具合并为一个并行 batch，其余各自单独 batch。"""
        batches: list[dict] = []
        for ct in checked:
            safe = ct["allowed"] and ct["fn"] in CONCURRENCY_SAFE_TOOLS
            if safe and batches and batches[-1]["concurrent"]:
                batches[-1]["items"].append(ct)
            else:
                batches.append({"concurrent": safe, "items": [ct]})
        return batches

    async def _run_single_tool(self, ct: dict) -> str:
        """执行单个工具，做大结果落盘 + UI 打印。"""
        raw = await self._execute_tool_call(ct["fn"], ct["inp"])
        res = persist_large_result(ct["fn"], raw)
        print_tool_result(ct["fn"], res)
        return res

    async def _execute_tool_batches(self, batches: list[dict]) -> None:
        """按 batch 顺序执行工具调用，处理 context-cleared 中断。"""
        context_break = False
        for batch in batches:
            if context_break or self._aborted:
                break

            if batch["concurrent"]:
                # 安全工具：并发执行
                async def _run(ct_item: dict) -> tuple[dict, str]:
                    return ct_item, await self._run_single_tool(ct_item)

                results = await asyncio.gather(*[_run(ct) for ct in batch["items"]])
                for ct_item, res in results:
                    self._openai_messages.append({
                        "role": "tool",
                        "tool_call_id": ct_item["tc"]["id"],
                        "content": res,
                    })
            else:
                for ct in batch["items"]:
                    if not ct["allowed"]:
                        # 被拒绝/未授权的工具直接把预设的 result 写回
                        self._openai_messages.append({
                            "role": "tool",
                            "tool_call_id": ct["tc"]["id"],
                            "content": ct["result"],
                        })
                        continue

                    res = await self._run_single_tool(ct)

                    # plan 审批走 clear-and-execute 时，需要把审批结果作为 user 消息回写并中断后续 batch
                    if self._context_cleared:
                        self._context_cleared = False
                        self._openai_messages.append({"role": "user", "content": res})
                        context_break = True
                        break

                    self._openai_messages.append({
                        "role": "tool",
                        "tool_call_id": ct["tc"]["id"],
                        "content": res,
                    })

        self._context_cleared = False

    # ─── OpenAI-compatible backend ───────────────────────────

    @observe()
    async def _chat_openai(self, user_message: str) -> None:
        # trace_name 可关联业务侧 ID，便于在 Langfuse 平台搜索
        with propagate_attributes(trace_name="123456789"):
            self._openai_messages.append({"role": "user", "content": user_message})

            # 启动异步记忆 prefetch（每用户回合一次，非阻塞）
            memory_prefetch: MemoryPrefetch | None = None
            if not self.is_sub_agent:
                sq = self._build_side_query()
                if sq:
                    memory_prefetch = start_memory_prefetch(
                        user_message, sq,
                        self._already_surfaced_memories, self._session_memory_bytes,
                    )

            while True:
                if self._aborted:
                    break

                # 每次发请求前先尝试压缩历史--多层管道压缩
                run_compression_pipeline(
                    self._openai_messages,
                    self.last_input_token_count,
                    self.effective_window,
                    self.last_api_call_time,
                )

                # 消费已就绪的记忆 prefetch
                if memory_prefetch:
                    self._consume_memory_prefetch(memory_prefetch)

                if not self.is_sub_agent:
                    start_spinner()
                response = await self._call_openai_stream()
                if not self.is_sub_agent:
                    stop_spinner()

                self.last_api_call_time = time.time()

                usage = response.get("usage")
                if usage:
                    self.total_input_tokens += usage["prompt_tokens"]
                    self.total_output_tokens += usage["completion_tokens"]
                    self.last_input_token_count = usage["prompt_tokens"]

                choice = response.get("choices", [{}])[0] if response.get("choices") else {}
                message = choice.get("message", {})
                self._openai_messages.append(message)

                tool_calls = message.get("tool_calls")
                if not tool_calls:
                    if not self.is_sub_agent:
                        print_cost(self.total_input_tokens, self.total_output_tokens)
                    break

                # 一个新轮次开始
                self.current_turns += 1
                budget = check_budget(
                    self.total_input_tokens, self.total_output_tokens,
                    self.current_turns, self.max_cost_usd, self.max_turns,
                )
                if budget["exceeded"]:
                    print_info(f"Budget exceeded: {budget['reason']}")
                    break

                # 阶段 1：串行做权限检查
                checked = await self._check_tool_calls(tool_calls)
                # 阶段 2：分组（连续的安全工具并行）执行
                batches = self._group_into_batches(checked)
                await self._execute_tool_batches(batches)

                await self._check_and_compact()

    async def _call_openai_stream(self) -> dict:
        """以流式方式调用 OpenAI，组装出与非流式格式兼容的响应字典。"""

        async def _do() -> dict:
            stream = await self._openai_client.chat.completions.create(
                model=self.model,
                tools=to_openai_tools(get_active_tool_definitions(self.tools)),
                messages=self._openai_messages,
                stream=True,
                stream_options={"include_usage": True},
            )

            content = ""
            first_text = True
            tool_calls: dict[int, dict] = {}
            finish_reason = ""
            usage: dict | None = None

            async for chunk in stream:
                if chunk.usage:
                    usage = {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    }

                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                # 流式文本
                if delta and delta.content:
                    if first_text:
                        stop_spinner()
                        self._emit_text("\n")
                        first_text = False
                    self._emit_text(delta.content)
                    content += delta.content

                # 流式工具调用（按 index 累积参数）
                if delta and delta.tool_calls:
                    for tc in delta.tool_calls:
                        existing = tool_calls.get(tc.index)
                        if existing:
                            if tc.function and tc.function.arguments:
                                existing["arguments"] += tc.function.arguments
                        else:
                            tool_calls[tc.index] = {
                                "id": tc.id or "",
                                "name": (tc.function.name if tc.function else "") or "",
                                "arguments": (tc.function.arguments if tc.function else "") or "",
                            }

                if chunk.choices[0].finish_reason:
                    finish_reason = chunk.choices[0].finish_reason

            assembled: list[dict] | None = None
            if tool_calls:
                assembled = [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {"name": tc["name"], "arguments": tc["arguments"]},
                    }
                    for _, tc in sorted(tool_calls.items())
                ]

            return {
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": content or None,
                        "tool_calls": assembled,
                    },
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0},
            }

        return await with_retry(_do)