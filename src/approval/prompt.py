"""System prompt construction — template embedded, variable interpolation, context gathering."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path
from datetime import date

from approval.memory import build_memory_prompt_section
from approval.skills import build_skill_descriptions
from approval.subagent import build_agent_descriptions
from approval.tools import get_deferred_tool_names

# ─── System prompt template (embedded) ──────────────────────

SYSTEM_PROMPT_TEMPLATE = """\
You are Approval Agent.
You are an interactive agent that helps users review, validate, and process business requests. Use the instructions below and the tools available to you to assist the user.

IMPORTANT: You must NEVER generate or guess URLs for the user. You may use URLs provided by the user in their messages or local files.

# System
 - The system will automatically compress prior messages in your conversation as it approaches context limits. This means your conversation with the user is not limited by the context window.

# Doing tasks
Skill First & Precise Execution: Before solving any problem, first check if there is an applicable Skill available; if a suitable Skill is found, prioritize retrieving and using its content to address the issue.
Precise Verification & Prevention of Divergence: The input includes Audit Details (business document context, URL links, remarks, etc.) and Audit Points (specific verification standards and criteria). Each audit point must strictly adhere to its description to extract the corresponding fields for verification; absolutely no additional divergence or extrapolation is allowed.
Handling Missing Items: If the information required for an audit point is not found in the current attachment, the reason must explicitly state: "Missing [Specific Category] Attachment."

# Using your tools
 - CRITICAL: Always prioritize using available skills to solve the problem before falling back on general tools.
 - Do NOT use the run_shell to run commands when a relevant dedicated tool is provided. Using dedicated tools allows the user to better understand and review your work. This is CRITICAL to assisting the user:
   - To read files use read_file instead of cat, head, tail, or sed
   - To create files use write_file instead of cat with heredoc or echo redirection
   - To search for files use list_files instead of find or ls
   - To search the content of files, use grep_search instead of grep or rg
   - Reserve using the run_shell exclusively for system commands and terminal operations that require shell execution. If you are unsure and there is a relevant dedicated tool, default to using the dedicated tool and only fallback on using the run_shell tool for these if it is absolutely necessary.
 - You can call multiple tools in a single response. If you intend to call multiple tools and there are no dependencies between them, make all independent tool calls in parallel. Maximize use of parallel tool calls where possible to increase efficiency. However, if some tool calls depend on previous calls to inform dependent values, do NOT call these tools in parallel and instead call them sequentially. For instance, if one operation must complete before another starts, run these operations sequentially instead.

# Tone and style
 - Your responses should be short and concise.

# Output efficiency
IMPORTANT: Go straight to the point. Try the simplest approach first without going in circles. Do not overdo it. Be extra concise.

Keep your text output brief and direct. Lead with the answer or action, not the reasoning. Skip filler words, preamble, and unnecessary transitions. Do not restate what the user said — just do it. When explaining, include only what is necessary for the user to understand.

If you can say it in one sentence, don't use three. Prefer short, direct sentences over long explanations. This does not apply to code or tool calls..

# Environment
Working directory: {{cwd}}
Platform: {{platform}}
Shell: {{shell}}
{{claude_md}}
{{memory}}
{{skills}}
{{agents}}
{{deferred_tools}}"""

import re as _re

# ─── @include resolution ─────────────────────────────────────
# Resolves @./path, @~/path, @/path references in CLAUDE.md files.

_INCLUDE_RE = _re.compile(r"^@(\./[^\s]+|~/[^\s]+|/[^\s]+)$", _re.MULTILINE)
_MAX_INCLUDE_DEPTH = 5


def _resolve_includes(
        content: str,
        base_path: Path,
        visited: set[str] | None = None,
        depth: int = 0,
) -> str:
    if depth >= _MAX_INCLUDE_DEPTH:
        return content
    if visited is None:
        visited = set()

    def _replace(m: _re.Match) -> str:
        raw = m.group(1)
        if raw.startswith("~/"):
            resolved = Path.home() / raw[2:]
        elif raw.startswith("/"):
            resolved = Path(raw)
        else:
            resolved = base_path / raw
        resolved = resolved.resolve()
        key = str(resolved)
        if key in visited:
            return f"<!-- circular: {raw} -->"
        if not resolved.is_file():
            return f"<!-- not found: {raw} -->"
        try:
            visited.add(key)
            included = resolved.read_text()
            return _resolve_includes(included, resolved.parent, visited, depth + 1)
        except Exception:
            return f"<!-- error reading: {raw} -->"

    return _INCLUDE_RE.sub(_replace, content)


def _load_rules_dir(directory: Path) -> str:
    """Load all .md files from .claude/rules/ directory."""
    rules_dir = directory / ".claude" / "rules"
    if not rules_dir.is_dir():
        return ""
    try:
        files = sorted(f for f in rules_dir.iterdir() if f.suffix == ".md" and f.is_file())
        if not files:
            return ""
        parts: list[str] = []
        for f in files:
            try:
                content = f.read_text()
                content = _resolve_includes(content, rules_dir)
                parts.append(f"<!-- rule: {f.name} -->\n{content}")
            except Exception:
                pass
        return "\n\n## Rules\n" + "\n\n".join(parts) if parts else ""
    except Exception:
        return ""


def load_claude_md() -> str:
    """Walk up from cwd collecting all CLAUDE.md files, resolving @includes."""
    parts: list[str] = []
    d = Path.cwd().resolve()
    while True:
        f = d / "CLAUDE.md"
        if f.is_file():
            try:
                content = f.read_text()
                content = _resolve_includes(content, d)
                parts.insert(0, content)
            except Exception:
                pass
        parent = d.parent
        if parent == d:
            break
        d = parent
    # Load .claude/rules/*.md from cwd
    rules = _load_rules_dir(Path.cwd())
    claude_md = ""
    if parts:
        claude_md = "\n\n# Project Instructions (CLAUDE.md)\n" + "\n\n---\n\n".join(parts)
    return claude_md + rules


def get_git_context() -> str:
    """Get git branch, recent commits, and status."""
    try:
        opts = {"encoding": "utf-8", "timeout": 3, "capture_output": True}
        branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], **opts).stdout.strip()
        log = subprocess.run(["git", "log", "--oneline", "-5"], **opts).stdout.strip()
        status = subprocess.run(["git", "status", "--short"], **opts).stdout.strip()
        result = f"\nGit branch: {branch}"
        if log:
            result += f"\nRecent commits:\n{log}"
        if status:
            result += f"\nGit status:\n{status}"
        return result
    except Exception:
        return ""


def build_system_prompt() -> str:
    """Build the full system prompt from embedded template + dynamic context."""
    plat = f"{platform.system()} {platform.machine()}"
    shell = (os.environ.get("ComSpec") or "cmd.exe") if sys.platform == "win32" else os.environ.get("SHELL", "/bin/sh")
    claude_md = load_claude_md()
    memory_section = build_memory_prompt_section()
    skills_section = build_skill_descriptions()
    agent_section = build_agent_descriptions()

    deferred_names = get_deferred_tool_names()
    deferred_section = (
        f"\n\nThe following deferred tools are available via tool_search: {', '.join(deferred_names)}. Use tool_search to fetch their full schemas when needed."
        if deferred_names else ""
    )

    replacements = {
        "{{cwd}}": str(Path.cwd()),
        "{{platform}}": plat,
        "{{shell}}": shell,
        "{{claude_md}}": claude_md,
        "{{memory}}": memory_section,
        "{{skills}}": skills_section,
        "{{agents}}": agent_section,
        "{{deferred_tools}}": deferred_section,
    }
    result = SYSTEM_PROMPT_TEMPLATE
    for key, value in replacements.items():
        result = result.replace(key, value)
    return result
