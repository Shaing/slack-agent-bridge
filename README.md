# cc-slack

Drive the Claude Code install on this machine from Slack. The bot connects to
Slack with **Socket Mode**, so the machine only needs outbound internet — no
public URL, port forwarding or tunnel service.

```
Slack (DM or @mention) ──(Socket Mode)──> cc-slack ──(Agent SDK)──> claude CLI on this machine
            ^                                                            │
            └──── Approve / Deny buttons  <──── permission prompts ──────┘
```

## Quick usage

### One Slack thread = one Claude Code session

```
you:  @aLLEN what's in this repo?          ← top-level message: starts a NEW session
 └─ aLLEN:  ✅ Done · 3 turns · 12s · $0.08
 └─ aLLEN:  This repo contains…
 └─ you:    now add a test for the parser   ← reply in the thread: SAME session, full context
```

* A new top-level message (DM, or `@aLLEN` in a channel) starts a new session.
* Replies inside that thread continue it — in channels you don't need to
  `@mention` again. Sessions survive bot restarts.
* Each thread has its **own** mode, model and working directory. Run several
  threads side by side (up to `CC_MAX_CONCURRENT`, default 3).

### Pick mode / model / directory when you start a thread

Put options **before** the prompt, in any order:

```
mode:plan  design a caching layer for the API
model:sonnet  summarize the last 10 commits
cwd:~/projects/foo  mode:acceptEdits  model:opus  fix the failing tests
```

Anything you leave out uses the defaults from `.env`
(`CC_DEFAULT_MODE`, `CC_MODEL`, `CC_DEFAULT_CWD`).

### Change them later, inside the thread

```
!mode plan        switch this thread's permission mode
!model haiku      switch this thread's model   (!model default = back to default)
!mode / !model    show the current value
!status           cwd, session, mode, model, running/idle
```

Changes apply **immediately**, even to a turn that is already running, and
only to that thread.

### All commands

| You type… | What happens |
|---|---|
| any text, top level | New session (options above optional); bot replies in a thread |
| reply in a thread | Continues that thread's session |
| `!mode [name]` | Show / set this thread's permission mode |
| `!model [name\|default]` | Show / set this thread's model: `sonnet`, `opus`, `haiku` or a full id |
| `!stop` | Interrupt the running turn in this thread |
| `!status` | In a thread: its settings and state. Top level: the 10 most recent threads (all users) |
| `!new` | Forget this thread's session (keeps cwd/mode/model); next message starts fresh |
| `!cwd /path` | Set cwd for a thread that hasn't started yet |
| `!help` | Command list with the current defaults |

## Permission modes

| Mode | What runs without asking you | Available by default |
|---|---|---|
| `default` | Only read-only actions; everything else gets Slack buttons | ✅ |
| `plan` | Exploration; edits and shell writes get buttons | ✅ |
| `acceptEdits` | File edits inside the cwd; other commands get buttons | ✅ |
| `auto` | Whatever a classifier approves; only some prompts reach Slack | opt-in |
| `dontAsk` | Only pre-approved rules; anything else is denied without asking | opt-in |
| `bypassPermissions` | Everything | opt-in |

Configure in `.env`:

```bash
CC_ALLOWED_MODES=default,plan,acceptEdits,auto   # what `!mode` / `mode:` may choose
CC_DEFAULT_MODE=auto                              # mode for new threads
```

When a prompt does reach Slack you get **Allow once / Always allow / Deny**.
Claude's clarifying questions arrive as buttons too (with *Other…* for a free-text
reply in the thread). After you press *Other…*, the next non-command message in
that thread is taken as the answer; `!stop`, `!status` and other `!commands`
still work in the meantime.

### What is per-thread and what is shared

| Setting | Scope | Stored in |
|---|---|---|
| mode, model, cwd, session | **one thread** | `state/threads.json` (the bot's own file) |
| *Always allow* rules | **everyone using that directory** — all threads with that cwd *and* your terminal `claude` there | `<cwd>/.claude/settings.local.json` |
| `~/.claude/settings.json` | global | never touched by the bot; its `defaultMode` is overridden per thread |

Mode and model are passed to Claude as flags on each turn; the bot never writes
them to a Claude config file.

## Security

* **Only allowlisted users** (`CC_ALLOWED_USERS`, Slack member IDs) are served.
  Anyone else gets "This bot is private." in DMs and is ignored in channels;
  their button clicks are refused.
* The Slack tokens are removed from the environment the `claude` CLI inherits,
  so commands Claude runs (e.g. `env`) can't see them.
* The `.env` file is the other place the tokens live, and Claude can read any
  file your user can. Keep it **outside** `CC_ALLOWED_ROOTS` — the default
  location `~/.config/cc-slack/.env` is chosen for that — and deny it to Claude
  in `~/.claude/settings.json` (the bot passes `setting_sources=["user", …]`,
  so this applies to every session it starts):

  ```json
  { "permissions": { "deny": ["Read(~/.config/cc-slack/**)"] } }
  ```

  That rule covers the `Read`/`Grep`/`Glob` tools. A shell command in `auto`
  or `bypassPermissions` mode could still `cat` the file, so use
  `default`/`plan` when Claude will process untrusted content (other people's
  repos, web pages).
* `cwd:` / `!cwd` only accept directories under `CC_ALLOWED_ROOTS`.
* In `auto` or `bypassPermissions`, commands run on this machine without your
  click. Prefer `default`/`plan` in shared channels, and review
  `<cwd>/.claude/settings.local.json` now and then — an *Always allow* rule there
  applies to every session in that directory.

## Setup

### 1. Create the Slack app (once)

1. <https://api.slack.com/apps> → **Create New App** → **From a manifest** → pick
   your workspace → paste `slack-manifest.yaml` → Create.
2. **Basic Information → App-Level Tokens → Generate** with scope
   `connections:write` → copy the `xapp-…` token.
3. **Install App** → install to workspace → copy the **Bot User OAuth Token**
   (`xoxb-…`).
4. Find your member ID: your Slack profile → ⋮ → **Copy member ID** (`U…`, not
   a `D…` channel ID).

If the DM says *"Sending messages to this app has been turned off"*: **App Home →
Show Tabs** → enable **Messages Tab** and tick *Allow users to send … messages*.

### 2. Configure & run

```bash
cd slack-agent                             # your clone of this repo
mkdir -p ~/.config/cc-slack && chmod 700 ~/.config/cc-slack
cp .env.example ~/.config/cc-slack/.env && chmod 600 ~/.config/cc-slack/.env
$EDITOR ~/.config/cc-slack/.env            # tokens, member ID, CC_DEFAULT_CWD, modes
uv sync
uv run cc-slack
```

Send the bot `hello`. You should see 👀 on your message, a status message,
Claude's reply, then ✅. The bot reads `.env` only at startup — restart it after
editing. It looks for `$CC_ENV_FILE`, then `~/.config/cc-slack/.env`, then
`./.env` (fine for a quick test, but see *Security* above). Run only **one**
instance at a time.

### Channels vs DM-only

The manifest enables channels: `/invite @aLLEN`, then `@aLLEN <prompt>`
(`CC_ALLOW_CHANNELS=1`). The bot can see messages in channels it's invited to,
but only acts on allowlisted users' mentions and thread replies. For DM-only,
comment out the "channel mode" lines in the manifest, reinstall the app, and set
`CC_ALLOW_CHANNELS=0`.

### Run as a service

`systemd/cc-slack.service` is a systemd user unit. Paths use `%h` (your home);
edit `WorkingDirectory` and the node directory in `PATH` for your machine first. It has no `EnvironmentFile=` on purpose:
cc-slack finds and parses its `.env` itself (see above), and systemd would keep
inline `# comments` as part of the values.

On a host with the `~/work/ops/svc` tool (where it is registered as `cc-slack`):

```bash
svc install cc-slack                  # link the unit into ~/.config/systemd/user and enable it
svc start cc-slack                    # or `svc adopt cc-slack` to replace a copy started by hand
svc logs cc-slack -f
```

Without it:

```bash
systemctl --user link "$PWD/systemd/cc-slack.service"   # symlink, so edits in the repo apply
systemctl --user enable --now cc-slack
journalctl --user -u cc-slack -f
```

Either way, run `loginctl enable-linger $USER` once so it keeps running after you
log out, and don't also start it with `uv run cc-slack` (two bots would answer every message).

## Tips

* **Cost** depends mostly on the model: a small task was ~$0.05 on `haiku` vs
  ~$0.15–0.25 on the default Fable/xhigh setup. Use `model:haiku` or
  `model:sonnet` for quick questions, or set `CC_MODEL`.
* Long answers are split across several Slack messages with code blocks kept
  intact.
* Slack's free plan works (Socket Mode, buttons, manifests are free). The 90-day
  history limit hides old threads in Slack, but the bot keeps
  `state/threads.json`, so those sessions still resume.

## Development

```bash
uv run pytest                                              # unit + offline integration tests
uv run python scripts/smoke_runner.py --cwd /tmp/x "…"     # drive the runner without Slack
uv run python scripts/smoke_runner.py --mode auto --auto n "…"   # try a mode; auto-answer prompts
```

Layout: `runner.py` (Agent SDK driver, no Slack), `stream.py` (throttled Slack
output), `permissions.py` (button prompts), `slack_app.py` (events, commands,
orchestration), `store.py` (thread → session JSON), `render.py` (markdown →
mrkdwn, Block Kit), `config.py` (`.env` settings).
