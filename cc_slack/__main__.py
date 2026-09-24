"""Entry point: `cc-slack` / `python -m cc_slack`."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.app.async_app import AsyncApp
from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler

from .config import ConfigError, Settings, load_dotenv
from .runner import Runner
from .slack_app import Bridge, register_handlers
from .store import SessionRegistry, ThreadStore

log = logging.getLogger("cc_slack")

SECRET_ENV = ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN")


async def _amain(settings: Settings) -> None:
    app = AsyncApp(token=settings.slack_bot_token, logger=logging.getLogger("slack_bolt"))
    app.client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=3))

    runner = Runner(
        cli_path=settings.cli_path,
        model=settings.model,
        max_concurrent=settings.max_concurrent,
        stream_deltas=settings.stream_deltas,
        turn_timeout_s=settings.turn_timeout_s,
    )
    sessions = SessionRegistry(ThreadStore(settings.state_file))
    bridge = Bridge(settings, app.client, runner, sessions)
    await bridge.startup()
    register_handlers(app, bridge)

    handler = AsyncSocketModeHandler(app, settings.slack_app_token)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    await handler.connect_async()
    log.info(
        "socket mode connected · cwd=%s · allowed users=%s · channels=%s",
        settings.default_cwd,
        ",".join(sorted(settings.allowed_users)),
        "on" if settings.allow_channels else "off (DM only)",
    )
    await stop.wait()
    log.info("shutting down…")
    await bridge.shutdown()
    await handler.close_async()


def main() -> None:
    load_dotenv(Path(".env"))
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    # The SDK hands os.environ to the claude CLI, so every command Claude runs
    # would see the Slack tokens (e.g. via `env`). Keep them only in Settings.
    for key in SECRET_ENV:
        os.environ.pop(key, None)
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("slack_bolt").setLevel(logging.WARNING)
    logging.getLogger("slack_sdk").setLevel(logging.WARNING)
    asyncio.run(_amain(settings))


if __name__ == "__main__":
    main()
