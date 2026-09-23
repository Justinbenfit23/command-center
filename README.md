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

macOS, Python 3.11+ (stdlib only), `tmux`, the `gh` CLI (authenticated), and Claude Code.
Optional: a Slack user token in the keychain, and an MCP ticket server on your PATH.

## Setup

```bash
cp cc/config.example.py cc/config.py   # fill in your roster, org, hosts
python3 server.py                      # http://127.0.0.1:8765
```

`cc/config.py` is gitignored: it holds your team roster, user ids and internal hostnames.

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
