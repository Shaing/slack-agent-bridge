# cc-slack

Drive the Claude Code install on this machine from Slack. The bot connects to
Slack with **Socket Mode**, so the machine only needs outbound internet — no
public URL, port forwarding or tunnel service.

```
Slack DM ──(Socket Mode)──> cc-slack (this repo) ──(Agent SDK)──> claude CLI on this machine
                 ^                                                     │
                 └── Approve / Deny buttons  <── permission prompts ───┘
```

* One Claude Code **session per Slack thread**: a new DM starts a session, replies
  in that thread continue it (survives bot restarts).
* Tool permission prompts and Claude's clarifying questions show up as
  **buttons** in the thread. Nothing runs on your machine without your click,
  unless you've chosen *Always allow* for that rule.
* **Private by default**: DM-only, and only the Slack member IDs in
  `CC_ALLOWED_USERS` are served. Anyone else gets "This bot is private."

## 1. Create the Slack app (once)

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest** → pick
   your workspace → paste `slack-manifest.yaml` → Create.
2. **Basic Information → App-Level Tokens → Generate** with scope
   `connections:write` → copy the `xapp-…` token.
3. **Install App** → install to workspace → copy the **Bot User OAuth Token**
   (`xoxb-…`).
4. Find your member ID: your Slack profile → ⋮ → **Copy member ID**.

## 2. Configure & run

```bash
cd /home/ah/work/slack-agent
cp .env.example .env && chmod 600 .env     # fill in the tokens, your member ID, CC_DEFAULT_CWD
uv sync
uv run cc-slack
```

Open the bot's DM in Slack and send `hello`. You should see 👀 on your message,
a status message, Claude's reply, then ✅.

## Usage

| You type… | What happens |
|---|---|
| any text (top level) | New session in `CC_DEFAULT_CWD`; bot replies in a thread |
| `cwd:/abs/path` + prompt | New session in that directory (must be under `CC_ALLOWED_ROOTS`) |
| reply in a thread | Continues that thread's session |
| `!stop` | Interrupt the running turn in this thread |
| `!status` | Session id, cwd, mode, state |
| `!new` | Forget the session (keep cwd); next message starts fresh |
| `!mode default\|plan\|acceptEdits` | Permission mode for later turns in this thread |
| `!cwd /path` | Set cwd for a thread that hasn't started yet |
| `!help` | Command list |

When Claude wants to run something that needs approval you get
**Allow once / Always allow / Deny**. *Always allow* writes the suggested rule to
`<cwd>/.claude/settings.local.json`, so later turns skip that prompt.

## Run as a service

```bash
cp systemd/cc-slack.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cc-slack
loginctl enable-linger $USER          # keep it running after you log out
journalctl --user -u cc-slack -f
```

## Development

```bash
uv run pytest                                             # unit + offline integration tests
uv run python scripts/smoke_runner.py --cwd /tmp/x "…"    # drive the runner without Slack
uv run python scripts/smoke_runner.py --auto y "…"        # auto-approve prompts
```

Layout: `runner.py` (Agent SDK driver, no Slack), `stream.py` (throttled Slack
output), `permissions.py` (button prompts), `slack_app.py` (events, commands,
orchestration), `store.py` (thread → session JSON), `render.py` (markdown →
mrkdwn, Block Kit).

## Notes

* Your `~/.claude/settings.json` sets `defaultMode: auto`; the bot forces
  `permission_mode=default` per turn so prompts reach Slack instead of the
  auto-classifier.
* Slack free plan is fine (Socket Mode, interactivity and manifests are all
  free). The 90-day history limit hides old threads in Slack, but the bot keeps
  its own `state/threads.json`, so those sessions still resume.
* To use the bot in channels, uncomment the marked scopes/events in the
  manifest, reinstall the app, `/invite` the bot, and set `CC_ALLOW_CHANNELS=1`.
