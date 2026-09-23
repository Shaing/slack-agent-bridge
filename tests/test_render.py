from cc_slack.render import md_to_mrkdwn, slack_text_to_plain, split_for_slack, summarize_tool_input


def test_bold_italic_heading():
    assert md_to_mrkdwn("# Title\n\nSome **bold** and *it* text") == "*Title*\n\nSome *bold* and _it_ text"


def test_links_and_escape():
    assert md_to_mrkdwn("see [docs](https://x.y/a?b=1&c=2) & <tag>") == "see <https://x.y/a?b=1&amp;c=2|docs> &amp; &lt;tag&gt;"


def test_fence_untouched_and_lang_dropped():
    src = "```python\nx = **not bold** <a>\n```"
    assert md_to_mrkdwn(src) == "```\nx = **not bold** <a>\n```"


def test_unterminated_fence_closed():
    out = md_to_mrkdwn("text\n```sh\necho hi")
    assert out.endswith("```") and out.count("```") == 2


def test_inline_code_preserved():
    assert md_to_mrkdwn("run `a && b` now") == "run `a && b` now"


def test_bullets_and_rule():
    assert md_to_mrkdwn("- one\n  - two\n---\n* three") == "• one\n  ◦ two\n\n• three"


def test_table_to_fence():
    out = md_to_mrkdwn("| a | b |\n|---|---|\n| 1 | 2 |")
    assert out == "```\n| a | b |\n| 1 | 2 |\n```"


def test_blockquote_kept():
    assert md_to_mrkdwn("> quoted > x") == "> quoted &gt; x"


def test_slack_text_to_plain():
    assert slack_text_to_plain("<@U1> hi <https://a.b|site> &amp; <@U2>", "U1") == "hi site (https://a.b) & @U2"


def test_split_balances_fences():
    body = "```\n" + ("line\n" * 400) + "```"
    chunks = split_for_slack(body, 1000)
    assert len(chunks) > 1
    for c in chunks:
        assert c.count("```") % 2 == 0
        assert len(c) <= 1010


def test_split_prefers_blank_line():
    text = ("para one " * 50).strip() + "\n\n" + ("para two " * 50).strip()
    chunks = split_for_slack(text, 600)
    assert chunks[0].startswith("para one") and chunks[1].startswith("para two")


def test_summarize_tool_input():
    s = summarize_tool_input("Bash", {"command": "git status\ngit log"})
    assert s == ":wrench: *Bash*  `git status`"
    s = summarize_tool_input("Read", {"file_path": "/repo/src/a.py"}, cwd="/repo")
    assert s.endswith("`src/a.py`")
    s = summarize_tool_input("mcp__github__get_issue", {"n": 1})
    assert s.startswith(":electric_plug: *github:get_issue*")


def test_heading_with_bold_inside():
    assert md_to_mrkdwn("## **Bold** heading") == "*Bold heading*"
