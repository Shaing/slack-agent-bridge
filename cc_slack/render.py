"""Pure rendering helpers: markdown -> Slack mrkdwn, tool summaries, Block Kit."""

from __future__ import annotations

import json
import os
import re
from typing import Any

FENCE_RE = re.compile(r"(```.*?```|```.*$)", re.S)
INLINE_CODE_RE = re.compile(r"`[^`\n]+`")


# --------------------------------------------------------------------------- #
# Markdown -> mrkdwn
# --------------------------------------------------------------------------- #
def _escape(text: str) -> str:
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    # keep leading '>' (blockquote) on each line, escape the rest
    lines = []
    for line in text.split("\n"):
        m = re.match(r"^(\s*>\s?)", line)
        head, rest = (m.group(1), line[m.end():]) if m else ("", line)
        lines.append(head + rest.replace(">", "&gt;"))
    return "\n".join(lines)


def _convert_prose(text: str) -> str:
    # protect inline code
    codes: list[str] = []

    def stash(m: re.Match[str]) -> str:
        codes.append(m.group(0))
        return f"\x00{len(codes) - 1}\x00"

    text = INLINE_CODE_RE.sub(stash, text)
    text = _escape(text)

    # links / images
    text = re.sub(
        r"!?\[([^\]]+)\]\((https?://[^)\s]+)\)",
        lambda m: f"<{m.group(2)}>" if m.group(1) == m.group(2) else f"<{m.group(2)}|{m.group(1)}>",
        text,
    )
    # italics (single * / _ ) before bold so they do not collide with bold output
    text = re.sub(r"(?<![*\w])\*(?!\*)([^*\n]+?)\*(?![*\w])", r"_\1_", text)
    # bold
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"*\1*", text, flags=re.S)
    # headings (after emphasis so the heading's own * survives)
    text = re.sub(
        r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$",
        lambda m: "*" + m.group(1).replace("*", "").strip() + "*",
        text,
        flags=re.M,
    )
    # strikethrough
    text = re.sub(r"~~(.+?)~~", r"~\1~", text, flags=re.S)
    # bullets
    text = re.sub(r"^(\s*)[-*+]\s+(?=\S)", lambda m: m.group(1) + ("◦ " if len(m.group(1)) >= 2 else "• "), text, flags=re.M)
    # horizontal rules
    text = re.sub(r"^\s*([-*_])\s*(\1\s*){2,}$", "", text, flags=re.M)

    # tables -> fenced block
    out_lines: list[str] = []
    table: list[str] = []

    def flush_table() -> None:
        if table:
            rows = [r for r in table if not re.match(r"^\s*\|?\s*:?-{2,}", r)]
            out_lines.append("```\n" + "\n".join(rows) + "\n```")
            table.clear()

    for line in text.split("\n"):
        if line.lstrip().startswith("|"):
            table.append(line.strip())
        else:
            flush_table()
            out_lines.append(line)
    flush_table()
    text = "\n".join(out_lines)

    # restore inline code
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _convert_fence(block: str) -> str:
    body = block[3:]
    closed = body.endswith("```")
    if closed:
        body = body[:-3]
    # drop the language tag on the opening line
    body = re.sub(r"^[\w+#.-]*[ \t]*\n", "\n", body, count=1) if "\n" in body else body
    if not body.startswith("\n"):
        body = "\n" + body
    if not body.endswith("\n"):
        body += "\n"
    return "```" + body + "```"


def md_to_mrkdwn(text: str) -> str:
    """Convert GitHub-flavoured markdown (as Claude writes it) to Slack mrkdwn."""
    parts = FENCE_RE.split(text)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if part.startswith("```"):
            out.append(_convert_fence(part))
        else:
            out.append(_convert_prose(part))
    return "".join(out).strip("\n")


# --------------------------------------------------------------------------- #
# Slack -> plain text (inbound)
# --------------------------------------------------------------------------- #
def slack_text_to_plain(text: str, bot_user_id: str | None = None) -> str:
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(\|[^>]*)?>", "", text)
    text = re.sub(r"<@([A-Z0-9]+)(\|[^>]*)?>", r"@\1", text)
    text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]*)>", r"\2 (\1)", text)
    text = re.sub(r"<(https?://[^>]+)>", r"\1", text)
    text = re.sub(r"<mailto:([^|>]+)(\|[^>]*)?>", r"\1", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text.strip()


# --------------------------------------------------------------------------- #
# Splitting long text for Slack
# --------------------------------------------------------------------------- #
def split_for_slack(text: str, limit: int) -> list[str]:
    """Split mrkdwn into chunks <= limit, keeping code fences balanced."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        head, rest = rest[:cut], rest[cut:].lstrip("\n")
        if head.count("```") % 2 == 1:
            head += "\n```"
            rest = "```\n" + rest
        chunks.append(head)
    if rest:
        chunks.append(rest)
    return chunks


# --------------------------------------------------------------------------- #
# Tool summaries
# --------------------------------------------------------------------------- #
TOOL_ICONS = {
    "Bash": ":wrench:",
    "Read": ":page_facing_up:",
    "Write": ":memo:",
    "Edit": ":pencil2:",
    "MultiEdit": ":pencil2:",
    "NotebookEdit": ":pencil2:",
    "Glob": ":mag:",
    "Grep": ":mag:",
    "WebFetch": ":globe_with_meridians:",
    "WebSearch": ":globe_with_meridians:",
    "Agent": ":robot_face:",
    "Task": ":robot_face:",
    "TodoWrite": ":ballot_box_with_check:",
    "AskUserQuestion": ":question:",
    "Skill": ":sparkles:",
}


def _rel(path: str | None, cwd: str | None) -> str:
    if not path:
        return ""
    if cwd:
        try:
            rel = os.path.relpath(path, cwd)
            if not rel.startswith(".."):
                return rel
        except ValueError:
            pass
    return path


def _first_line(s: str, n: int = 120) -> str:
    line = s.strip().split("\n", 1)[0]
    return line if len(line) <= n else line[: n - 1] + "…"


def summarize_tool_input(name: str, inp: dict[str, Any], cwd: str | None = None) -> str:
    """One-line summary for the activity log."""
    icon = TOOL_ICONS.get(name, ":gear:")
    label = name
    if name.startswith("mcp__"):
        parts = name.split("__")
        label = ":".join(parts[1:3]) if len(parts) >= 3 else name
        icon = ":electric_plug:"
    if name == "Bash":
        detail = _first_line(inp.get("command", ""))
    elif name in ("Read", "Write", "Edit", "MultiEdit", "NotebookEdit"):
        detail = _rel(inp.get("file_path") or inp.get("notebook_path"), cwd)
    elif name in ("Glob", "Grep"):
        detail = inp.get("pattern", "")
        if inp.get("path"):
            detail += f" in {_rel(inp['path'], cwd)}"
    elif name in ("WebFetch",):
        detail = inp.get("url", "")
    elif name == "WebSearch":
        detail = inp.get("query", "")
    elif name in ("Agent", "Task"):
        detail = _first_line(inp.get("description") or inp.get("prompt", ""), 80)
    elif name == "TodoWrite":
        detail = "updated todos"
    elif name == "Skill":
        detail = inp.get("skill", "")
    else:
        detail = _first_line(json.dumps(inp, ensure_ascii=False), 100)
    detail = detail.replace("\n", " ")
    return f"{icon} *{label}*  `{detail}`" if detail else f"{icon} *{label}*"


def summarize_tool_input_full(name: str, inp: dict[str, Any], cwd: str | None = None, limit: int = 1500) -> str:
    """Multi-line preview shown inside a permission prompt."""
    if name == "Bash":
        body = inp.get("command", "")
    elif name == "Edit":
        body = (
            f"{_rel(inp.get('file_path'), cwd)}\n--- old\n{inp.get('old_string', '')}\n+++ new\n{inp.get('new_string', '')}"
        )
    elif name == "Write":
        content = inp.get("content", "")
        body = f"{_rel(inp.get('file_path'), cwd)}\n{content}"
    elif name in ("Read", "Glob", "Grep", "WebFetch", "WebSearch"):
        body = summarize_tool_input(name, inp, cwd)
        return body  # already mrkdwn, no fence
    else:
        body = json.dumps(inp, indent=2, ensure_ascii=False)
    if len(body) > limit:
        body = body[: limit - 1] + "…"
    body = body.replace("```", "'''")
    return f"```\n{body}\n```"


# --------------------------------------------------------------------------- #
# Block Kit builders
# --------------------------------------------------------------------------- #
def _section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}


def _context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text[:3000]}]}


def _button(text: str, action_id: str, value: str, style: str | None = None) -> dict[str, Any]:
    btn: dict[str, Any] = {
        "type": "button",
        "text": {"type": "plain_text", "text": text[:75], "emoji": True},
        "action_id": action_id,
        "value": value,
    }
    if style:
        btn["style"] = style
    return btn


def permission_blocks(
    prompt_id: str,
    tool_name: str,
    inp: dict[str, Any],
    *,
    cwd: str | None,
    title: str | None,
    description: str | None,
    always_rule: str | None,
) -> list[dict[str, Any]]:
    header = f":lock: *{tool_name}* — {title}" if title else f":lock: *{tool_name}* wants to run"
    blocks: list[dict[str, Any]] = [_section(header)]
    if description:
        blocks.append(_context(description[:500]))
    blocks.append(_section(summarize_tool_input_full(tool_name, inp, cwd)))
    buttons = [_button("Allow once", "cc_perm_allow", prompt_id, "primary")]
    if always_rule:
        buttons.append(_button("Always allow", "cc_perm_always", prompt_id))
    buttons.append(_button("Deny", "cc_perm_deny", prompt_id, "danger"))
    blocks.append({"type": "actions", "block_id": f"cc_perm_{prompt_id}", "elements": buttons})
    if always_rule:
        blocks.append(_context(f"_Always allow_ adds rule `{always_rule}` to `.claude/settings.local.json`"))
    return blocks


def question_blocks(
    prompt_id: str,
    questions: list[dict[str, Any]],
    selections: dict[int, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Render AskUserQuestion. Single-select -> buttons; multiSelect -> checkboxes.

    A Submit button is added when more than one question or any multiSelect.
    """
    selections = selections or {}
    blocks: list[dict[str, Any]] = [_section(":question: *Claude has a question*")]
    needs_submit = len(questions) > 1 or any(q.get("multiSelect") for q in questions)
    for qi, q in enumerate(questions):
        picked = selections.get(qi, [])
        head = f"*{q.get('header', 'Question')}* — {q.get('question', '')}"
        if picked:
            head += f"\n:white_check_mark: _{', '.join(picked)}_"
        blocks.append(_section(head))
        options = q.get("options", [])[:4]
        if q.get("multiSelect"):
            opts = [
                {
                    "text": {"type": "plain_text", "text": o["label"][:75]},
                    "description": {"type": "plain_text", "text": (o.get("description") or " ")[:75]},
                    "value": str(oi),
                }
                for oi, o in enumerate(options)
            ]
            elem: dict[str, Any] = {"type": "checkboxes", "action_id": f"cc_q_multi_{qi}", "options": opts}
            initial = [o for oi, o in enumerate(opts) if options[oi]["label"] in picked]
            if initial:
                elem["initial_options"] = initial
            blocks.append({"type": "actions", "block_id": f"cc_q_{prompt_id}_{qi}", "elements": [elem]})
        else:
            elems = [
                _button(
                    o["label"],
                    f"cc_q_pick_{qi}_{oi}",
                    json.dumps({"p": prompt_id, "q": qi, "o": oi}),
                    "primary" if o["label"] in picked else None,
                )
                for oi, o in enumerate(options)
            ]
            elems.append(_button("Other…", f"cc_q_other_{qi}", json.dumps({"p": prompt_id, "q": qi})))
            blocks.append({"type": "actions", "block_id": f"cc_q_{prompt_id}_{qi}", "elements": elems})
            descs = [f"• *{o['label']}* — {o.get('description', '')}" for o in options if o.get("description")]
            if descs:
                blocks.append(_context("\n".join(descs)))
    if needs_submit:
        blocks.append(
            {
                "type": "actions",
                "block_id": f"cc_q_{prompt_id}_submit",
                "elements": [_button("Submit answers", "cc_q_submit", prompt_id, "primary")],
            }
        )
    return blocks


def resolved_blocks(text: str) -> list[dict[str, Any]]:
    return [_context(text)]
