# Command Center

A local, single-file-server dashboard for running many Claude Code sessions without losing track of them.

One list of work items. A work item **is** a Claude Code session: live tmux sessions show up
automatically, and a new item can start its own. Each row carries the links that matter (ticket,
spec doc, source thread), a lift tag that sorts the list, and an inline chat that reads the
session transcript and types into the real tmux pane.

## What it does

- **Work items** — paste a Slack link or type an ask. It pulls the prior 24h of that
  conversation (channel, DM or group DM) and seeds a new Claude session with a JSON context block.
- **Live sessions** — every running interactive Claude Code session appears as a row, with
  busy/idle state, the transcript (Markdown rendered, tool calls hidden), and a box that sends
  into the real pane. Stop sends Esc.
- **Link pinning** — ticket keys, doc URLs, PRs and Slack links that appear in a session's output
  get pinned into fixed slots on the row instead of scrolling away in chat.
- **My PRs** — grouped by repo, with CI/review state, Update branch, Auto-merge, and a Monitor
  button that opens a session running a PR-monitor skill in that repo.
- **Team** — commits per day per engineer plus their open tickets, with a load estimate.
- **Roadmap** — big rocks, tickets mapped under each with a one-line rationale, and a Mermaid graph.

## Requirements

macOS only (tmux, the keychain, and AppleScript are used directly). Python 3.11+ with the standard
library, `tmux`, the `gh` CLI (authenticated), and Claude Code. Everything else is optional and
degrades to an empty tab or a one-line error when it is missing.

## Setup

```bash
git clone https://github.com/Justinbenfit23/command-center.git
cd command-center
cp cc/config.example.py cc/config.py   # fill in your login, roster, hosts; or let Claude do it
python3 server.py                      # http://127.0.0.1:8765
```

Fastest path: run `claude` inside the folder and ask it to read `README.md` and
`cc/config.example.py`, then write `cc/config.py` for you. `cc/config.py` is gitignored: it holds
your team roster, user ids, internal hostnames and the prompts below.

## Make it yours

The app is a wrapper around Claude Code sessions. Everything that was specific to the author's
workflow is a value in `cc/config.py`, so swapping in your own skills is editing strings, not code:

| Config key | What it drives | Default in the example |
|---|---|---|
| `TICKET_PREFIX`, `TICKET_SYSTEM_NAME` | which keys get pinned onto cards (`PREFIX-123`), how prompts name your tracker | `PROJ`, `ticket` |
| `SEED_ASK_NEW`, `SEED_ASK_TICKET` | the instruction a new session gets with the intake JSON | plain English, no tracker required |
| `PRD_COMMAND`, `DONE_COMMAND`, `PR_MONITOR_COMMAND` | what the PRD, Done and Monitor buttons type into the pane; put your own `/skill` names here | plain English |
| `CLAUDE_CWD` | the folder new sessions start in (where your MCP servers and CLAUDE.md are registered) | `~/work` |
| `CHECKPOINTS_CMD` | a command printing today's "checkpoint" calendar events, one per line | `None` (strip off) |
| `TEAM`, `TEAM_ID`, `ME_UID`, `HANDOFF_TEMPLATE` | the Team tab and the one-click handoff | placeholders |
| `SHIPYARD_HOST`, `RADAR_HOST`, `SLACK_HOST`, `GH_ORG` | hosts the app builds links for and matches against | example hosts |
| `LD_TAG`, `LD_PROJECT`, `LD_ENV` | the feature-flag check in the Team audit; skipped without a token | placeholder |
| `ROCKS` | the Roadmap tab's big rocks | two placeholders |

Optional integrations, each one off until its dependency exists: a ticket MCP server named
`shipyard-mcp-stdio` on your PATH (tickets, PRD links, Team tab, handoff); a Slack user token
reachable through the scripts folder in `SLACK_SCRIPTS` (Slack intake and DMs); a LaunchDarkly read
token in `LD_API_KEY` or `~/.ld_env` (flag audit); a roadmap CLI at `ROADMAP_CLI` (the EPD board).
The usage card reads your Claude Code login from the macOS keychain and works for anyone.

## Layout

```
server.py            HTTP server and all routes
cc/config.py         your roster, org, hosts        (gitignored)
cc/db.py             SQLite store
cc/live.py           tmux session discovery, transcripts, send-keys
cc/slack.py          Slack Web API reads + DM
cc/github.py         gh CLI wrappers
cc/shipyard.py       ticket MCP client over stdio JSON-RPC
cc/roadmap.py        big-rock mapping
cc/triage.py         rules-first scoring
static/index.html    the whole UI
```

Data lives in `~/.command-center/`.
