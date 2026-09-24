"""Run one Claude Code turn through cc_slack.runner without Slack.

    uv run python scripts/smoke_runner.py --cwd /path "prompt"
    uv run python scripts/smoke_runner.py --resume <session_id> "follow-up"
    uv run python scripts/smoke_runner.py --auto y "..."   # auto-answer prompts (y/a/n)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from cc_slack.render import summarize_tool_input, summarize_tool_input_full  # noqa: E402
from cc_slack.runner import Decision, QuestionAnswer, Runner, TurnRequest, TurnResult  # noqa: E402


class PrintSink:
    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.last: dict[int, str] = {}

    async def on_session_started(self, session_id: str) -> None:
        print(f"[session] {session_id}")

    async def on_text(self, segment: int, text: str) -> None:
        prev = self.last.get(segment, "")
        if text.startswith(prev):
            sys.stdout.write(text[len(prev):])
        else:
            sys.stdout.write("\n" + text)
        sys.stdout.flush()
        self.last[segment] = text

    async def on_tool_use(self, tool_use_id: str, name: str, inp: dict, subagent: bool) -> None:
        indent = "    " if subagent else ""
        print(f"\n{indent}{summarize_tool_input(name, inp, self.cwd)}")

    async def on_tool_result(self, tool_use_id: str, is_error: bool) -> None:
        if is_error:
            print("   ↳ tool error")

    async def on_waiting(self, kind: str, tool_name: str) -> None:
        print(f"\n[waiting] {kind} for {tool_name}")

    async def on_working(self) -> None:
        print("[working]")

    async def on_notice(self, text: str) -> None:
        print(f"\n[notice] {text}")

    async def on_result(self, result: TurnResult) -> None:
        print(
            f"\n\n[result] subtype={result.subtype} error={result.error!r} turns={result.num_turns} "
            f"cost=${result.total_cost_usd or 0:.4f} {result.duration_ms}ms\n[session] {result.session_id}"
        )
        if result.stderr_tail:
            print(f"[stderr]\n{result.stderr_tail}")


class StdinPrompter:
    def __init__(self, auto: str | None) -> None:
        self.auto = auto

    async def _read(self, prompt: str) -> str:
        if self.auto:
            print(f"{prompt}{self.auto}  (auto)")
            return self.auto
        return (await asyncio.to_thread(input, prompt)).strip()

    async def ask_permission(self, thread_key, tool_name, input_data, context) -> Decision:
        print(f"\n[PERMISSION] {tool_name} title={context.title!r} suggestions={len(context.suggestions or [])}")
        print(summarize_tool_input_full(tool_name, input_data))
        ans = (await self._read("allow? [y]es / [a]lways / [n]o: ")).lower()
        if ans.startswith("a"):
            return Decision("always", "stdin")
        if ans.startswith("y"):
            return Decision("allow", "stdin")
        return Decision("deny", "stdin")

    async def ask_question(self, thread_key, questions) -> QuestionAnswer:
        answers = {}
        for q in questions:
            print(f"\n[QUESTION] {q.get('header')}: {q.get('question')}")
            for i, o in enumerate(q.get("options", [])):
                print(f"  {i + 1}. {o['label']} — {o.get('description', '')}")
            raw = await self._read("choice (number or text): ")
            opts = q.get("options", [])
            try:
                idx = [int(x) - 1 for x in raw.split(",")]
                labels = [opts[i]["label"] for i in idx if 0 <= i < len(opts)]
                answers[q["question"]] = labels if q.get("multiSelect") else labels[0]
            except (ValueError, IndexError):
                answers[q["question"]] = raw
        return QuestionAnswer(answers=answers, by_user="stdin")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt")
    ap.add_argument("--cwd", default=os.getcwd())
    ap.add_argument("--resume")
    ap.add_argument("--mode", default="default")
    ap.add_argument("--auto", choices=["y", "a", "n"])
    ap.add_argument("--cli", default=os.environ.get("CC_CLI_PATH"))
    ap.add_argument("--stream", action="store_true")
    ap.add_argument("-v", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cwd = os.path.realpath(args.cwd)
    runner = Runner(cli_path=args.cli, stream_deltas=args.stream)
    req = TurnRequest("smoke", args.prompt, cwd, args.resume, args.mode)
    await runner.run_turn(req, PrintSink(cwd), StdinPrompter(args.auto))


if __name__ == "__main__":
    asyncio.run(main())
