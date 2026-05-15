"""Shared YAML frontmatter parser for memory and skills files.
Handles simple `key: value` pairs between `---` delimiters."""

from dataclasses import dataclass, field


# skill markdown举例
# ---
# name: commit
# description: Create a git commit with a descriptive message
# when_to_use: When the user asks to commit changes or says "commit"
# allowed-tools: run_shell, read_file
# user-invocable: true
# ---
# Look at the current git diff and staged changes. Write a clear, concise
# commit message following conventional commits format.
#
# The user's request: $ARGUMENTS
#
# Project skill directory: ${CLAUDE_SKILL_DIR}

@dataclass
class FrontmatterResult:
    meta: dict[str, str] = field(default_factory=dict)
    body: str = ""


# 这段代码实现了 Frontmatter 的解析逻辑。Frontmatter 是一种常见的文件格式规范（起源于 Jekyll 等静态网站生成器），允许用户在文件开头通过一对 --- 分隔符定义元数据（Metadata）。
def parse_frontmatter(content: str) -> FrontmatterResult:
    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return FrontmatterResult(body=content)

    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        return FrontmatterResult(body=content)

    meta: dict[str, str] = {}
    for i in range(1, end_idx):
        colon_idx = lines[i].find(":")
        if colon_idx == -1:
            continue
        key = lines[i][:colon_idx].strip()
        value = lines[i][colon_idx + 1:].strip()
        if key:
            meta[key] = value

    body = "\n".join(lines[end_idx + 1:]).strip()
    return FrontmatterResult(meta=meta, body=body)


# 将内存中的字典（元数据）和字符串（正文）重新组合，还原成符合 Frontmatter 规范的 Markdown 文本。
def format_frontmatter(meta: dict[str, str], body: str) -> str:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    return "\n".join(lines)
