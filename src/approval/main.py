"""CLI entry point and interactive REPL — mirrors cli.ts."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys

from approval.agent import Agent
from approval.ui import print_welcome, print_user_prompt, print_error, print_info, print_plan_for_approval, \
    print_plan_approval_options
from approval.memory import list_memories
from approval.skills import discover_skills, resolve_skill_prompt, get_skill_by_name, execute_skill
from dotenv import load_dotenv

load_dotenv()


async def run_repl(agent: Agent) -> None:
    """Interactive REPL loop."""

    async def confirm_fn(message: str) -> bool:
        try:
            answer = input("  Allow? (y/n): ")
            return answer.lower().startswith("y")
        except EOFError:
            return False

    agent.set_confirm_fn(confirm_fn)

    async def plan_approval_fn(plan_content: str) -> dict:
        print_plan_for_approval(plan_content)
        print_plan_approval_options()
        while True:
            try:
                choice = input("  Enter choice (1-4): ").strip()
            except EOFError:
                return {"choice": "manual-execute"}
            if choice == "1":
                return {"choice": "clear-and-execute"}
            elif choice == "2":
                return {"choice": "execute"}
            elif choice == "3":
                return {"choice": "manual-execute"}
            elif choice == "4":
                try:
                    feedback = input("  Feedback (what to change): ").strip()
                except EOFError:
                    feedback = ""
                return {"choice": "keep-planning", "feedback": feedback or None}
            else:
                print("  Invalid choice. Enter 1, 2, 3, or 4.")

    agent.set_plan_approval_fn(plan_approval_fn)

    sigint_count = 0

    def handle_sigint(sig, frame):
        nonlocal sigint_count
        if agent._aborted is False and agent._output_buffer is not None:
            # Agent is processing
            agent.abort()
            print("\n  (interrupted)")
            sigint_count = 0
            print_user_prompt()
        else:
            sigint_count += 1
            if sigint_count >= 2:
                print("\nBye!\n")
                sys.exit(0)
            print("\n  Press Ctrl+C again to exit.")
            print_user_prompt()

    signal.signal(signal.SIGINT, handle_sigint)
    print_welcome()

    while True:
        print_user_prompt()
        try:
            line = input()
        except (EOFError, KeyboardInterrupt):
            print("\nBye!\n")
            break

        inp = line.strip()
        sigint_count = 0

        if not inp:
            continue
        if inp in ("exit", "quit"):
            print("\nBye!\n")
            break

        # REPL commands
        if inp == "/clear":
            agent.clear_history()
            continue
        if inp == "/plan":
            agent.toggle_plan_mode()
            continue
        if inp == "/cost":
            agent.show_cost()
            continue
        if inp == "/compact":
            try:
                await agent.compact()
            except Exception as e:
                print_error(str(e))
            continue
        if inp == "/memory":
            memories = list_memories()
            if not memories:
                print_info("No memories saved yet.")
            else:
                print_info(f"{len(memories)} memories:")
                for m in memories:
                    print(f"    [{m.type}] {m.name} — {m.description}")
            continue
        if inp == "/skills":
            skills = discover_skills()
            if not skills:
                print_info("No skills found. Add skills to .claude/skills/<name>/SKILL.md")
            else:
                print_info(f"{len(skills)} skills:")
                for s in skills:
                    tag = f"/{s.name}" if s.user_invocable else s.name
                    print(f"    {tag} ({s.source}) — {s.description}")
            continue

        # Skill invocation: /<skill-name> [args]
        if inp.startswith("/"):
            space_idx = inp.find(" ")
            cmd_name = inp[1:space_idx] if space_idx > 0 else inp[1:]
            cmd_args = inp[space_idx + 1:] if space_idx > 0 else ""
            skill = get_skill_by_name(cmd_name)
            if skill and skill.user_invocable:
                print_info(f"Invoking skill: {skill.name}")
                try:
                    if skill.context == "fork":
                        result = execute_skill(skill.name, cmd_args)
                        if result:
                            await agent.chat(
                                f'Use the skill tool to invoke "{skill.name}" with args: {cmd_args or "(none)"}')
                    else:
                        resolved = resolve_skill_prompt(skill, cmd_args)
                        await agent.chat(resolved)
                except Exception as e:
                    if "abort" not in str(e).lower():
                        print_error(str(e))
                continue

        # Normal chat
        try:
            await agent.chat(inp)
        except Exception as e:
            if "abort" not in str(e).lower():
                print_error(str(e))


def main() -> None:
    permission_mode = "default"
    model = os.environ.get("MINI_MODEL")

    # Resolve API config
    api_base: str | None = None
    api_key: str | None = None

    if os.environ.get("OPENAI_API_KEY") and os.environ.get("OPENAI_BASE_URL"):
        api_key = os.environ["OPENAI_API_KEY"]
        api_base = os.environ.get("OPENAI_BASE_URL")

    if not api_key or not api_base:
        print_error(
            "API key is required.\n"
            "  Set OPENAI_API_KEY + OPENAI_BASE_URL for OpenAI-compatible format."
        )
        sys.exit(1)

    agent = Agent(
        permission_mode=permission_mode,
        model=model,
        thinking=True,
        api_base=api_base,
        api_key=api_key,
    )
    # Interactive REPL
    asyncio.run(run_repl(agent))


if __name__ == "__main__":
    main()
