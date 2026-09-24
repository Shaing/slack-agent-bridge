# cc-slack

See README.md for usage and layout.

## Service management

Registered with `~/work/ops/svc` as `cc-slack` (`KIND=user-systemd`, unit file `systemd/cc-slack.service`,
linked into `~/.config/systemd/user` by `svc install cc-slack`). Use `svc status|logs|restart cc-slack`;
do not run a second copy with `uv run cc-slack` while the unit is active (two bots answer every message).

Sessions started from Slack are children of this process. From such a session never stop, restart or
adopt cc-slack — `svc` refuses; ask the user to run it from a normal terminal. After changing code,
the user restarts it with `svc restart cc-slack`.

The unit has no `EnvironmentFile=`: cc-slack loads its `.env` itself (`$CC_ENV_FILE`, else
`~/.config/cc-slack/.env`, else `./.env`), and systemd would keep inline `# comments` as part of the
values. Keep it that way.

The live `.env` is `~/.config/cc-slack/.env` on purpose: outside `CC_ALLOWED_ROOTS`, and denied to
Claude by a `Read(~/.config/cc-slack/**)` rule in `~/.claude/settings.json`. Never copy it back into
the repo directory, and never read or print it from a session.
