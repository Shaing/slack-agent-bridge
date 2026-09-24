"""Settings loaded from the environment (and an optional .env file)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, '#' comments, no interpolation.

    Existing environment variables win over the file.
    """
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()  # drop trailing inline comment
        os.environ.setdefault(key, value)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool = False) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "on")


# Modes that still route every risky action to a human in Slack.
SAFE_MODES = ("default", "plan", "acceptEdits")
# `auto` lets a classifier approve actions; `bypassPermissions` approves everything.
ALL_MODES = (*SAFE_MODES, "auto", "dontAsk", "bypassPermissions")


class ConfigError(Exception):
    pass


@dataclass
class Settings:
    slack_bot_token: str
    slack_app_token: str
    allowed_users: frozenset[str]
    default_cwd: str
    allowed_roots: tuple[str, ...]
    cli_path: str | None = None
    state_file: Path = Path("state/threads.json")
    max_concurrent: int = 3
    prompt_timeout_s: float = 3600
    edit_interval_s: float = 1.5
    msg_max_chars: int = 3500
    allow_channels: bool = False
    show_tools: bool = True
    stream_deltas: bool = False
    turn_timeout_s: float = 0
    model: str | None = None
    default_mode: str = "default"
    allowed_modes: tuple[str, ...] = SAFE_MODES
    log_level: str = "INFO"
    extra_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> Settings:
        missing = [k for k in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN") if not _env(k)]
        if missing:
            raise ConfigError(f"missing required env: {', '.join(missing)}")

        allowed_users = frozenset(
            u.strip() for u in (_env("CC_ALLOWED_USERS") or "").split(",") if u.strip()
        )
        if not allowed_users:
            raise ConfigError("CC_ALLOWED_USERS must list at least one Slack member ID")

        default_cwd = os.path.realpath(os.path.expanduser(_env("CC_DEFAULT_CWD") or os.getcwd()))
        if not os.path.isdir(default_cwd):
            raise ConfigError(f"CC_DEFAULT_CWD is not a directory: {default_cwd}")

        roots_raw = _env("CC_ALLOWED_ROOTS") or default_cwd
        allowed_roots = tuple(
            os.path.realpath(os.path.expanduser(r.strip()))
            for r in roots_raw.split(":")
            if r.strip()
        )

        allowed_modes = tuple(
            m.strip() for m in (_env("CC_ALLOWED_MODES") or ",".join(SAFE_MODES)).split(",") if m.strip()
        )
        bad = [m for m in allowed_modes if m not in ALL_MODES]
        if bad:
            raise ConfigError(f"CC_ALLOWED_MODES has unknown modes {bad}; valid: {', '.join(ALL_MODES)}")
        default_mode = _env("CC_DEFAULT_MODE") or "default"
        if default_mode not in allowed_modes:
            raise ConfigError(f"CC_DEFAULT_MODE={default_mode!r} is not in CC_ALLOWED_MODES")

        cli_path = _env("CC_CLI_PATH")
        if cli_path is None:
            candidate = Path.home() / ".local/bin/claude"
            cli_path = str(candidate) if candidate.exists() else None
        if cli_path is not None and not Path(cli_path).exists():
            raise ConfigError(f"CC_CLI_PATH does not exist: {cli_path}")

        return cls(
            slack_bot_token=_env("SLACK_BOT_TOKEN"),  # type: ignore[arg-type]
            slack_app_token=_env("SLACK_APP_TOKEN"),  # type: ignore[arg-type]
            allowed_users=allowed_users,
            default_cwd=default_cwd,
            allowed_roots=allowed_roots,
            cli_path=cli_path,
            state_file=Path(_env("CC_STATE_FILE") or "state/threads.json"),
            max_concurrent=int(_env("CC_MAX_CONCURRENT") or 3),
            prompt_timeout_s=float(_env("CC_PROMPT_TIMEOUT_S") or 3600),
            edit_interval_s=float(_env("CC_EDIT_INTERVAL_S") or 1.5),
            msg_max_chars=int(_env("CC_MSG_MAX_CHARS") or 3500),
            allow_channels=_env_bool("CC_ALLOW_CHANNELS"),
            show_tools=_env_bool("CC_SHOW_TOOLS", True),
            stream_deltas=_env_bool("CC_STREAM_DELTAS"),
            turn_timeout_s=float(_env("CC_TURN_TIMEOUT_S") or 0),
            model=_env("CC_MODEL"),
            default_mode=default_mode,
            allowed_modes=allowed_modes,
            log_level=_env("CC_LOG_LEVEL") or "INFO",
        )


def resolve_cwd(path: str, allowed_roots: tuple[str, ...]) -> str:
    """Validate a user-supplied working directory.

    Returns the realpath. Raises ConfigError if it does not exist or is not
    inside one of the allowed roots.
    """
    real = os.path.realpath(os.path.expanduser(path.strip()))
    if not os.path.isdir(real):
        raise ConfigError(f"not a directory: {real}")
    for root in allowed_roots:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return real
    roots = ", ".join(f"`{r}`" for r in allowed_roots)
    raise ConfigError(f"`{real}` is outside the allowed roots ({roots})")
