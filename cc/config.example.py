"""Static config. Copy to cc/config.py and fill in. cc/config.py is gitignored on purpose."""
import os

HOME = os.path.expanduser("~")
DB_PATH = os.path.expanduser("~/.command-center/cc.sqlite")
GH_ORG = "your-github-org"
ME = "your-github-login"
ME_UID = "00000000-0000-0000-0000-00000000000f"
TEAM_ID = "00000000-0000-0000-0000-0000000000aa"   # Shipyard GTM Products

# The engineers whose bandwidth this tracks.
TEAM = {
    # login -> (display name, Shipyard user uid, Slack user id)
    "engineer-one_example":   ("Engineer One",   "00000000-0000-0000-0000-000000000001", "U000000001"),
    "engineer-two_example":   ("Engineer Two",   "00000000-0000-0000-0000-000000000002", "U000000002"),
}

# The handoff DM: the PRD link and a bandwidth ask. No framing, no title, no ticket link.
HANDOFF_TEMPLATE = (
    "{prd_url}\n\n"
    "Can you confirm you have bandwidth to take this on?"
)
NOT_ASSIGNABLE = set()

# The big rocks. Everything on the roadmap hangs off one of these.
ROCKS = [
    {"id": "rock_one", "title": "First big rock", "blurb": "What it means and why it matters."},
    {"id": "rock_two", "title": "Second big rock", "blurb": "What it means and why it matters."},
]

# From dbr-workflow-experiment §6. Lowercase display-name substrings -> tier.
VIP = {
    # Lowercase display-name substrings -> tier. Drives urgency scoring on intake.
    "csuite": [],
    "vp": [],
    "director": [],
    "manager": [],
}
FIRE_DRILL_CHANNELS = {"your-feedback-channel"}
CHANNEL_NAMES = {"C00000000": "your-channel"}

LOCAL_CI_SCRIPT_DIRS = [
    os.path.expanduser("~/.claude/plugins/cache/your-plugins/exp-local-ci/0.9.0/scripts"),
    os.path.expanduser("~/path/to/plugin-marketplace/experimental/exp-local-ci/scripts"),
]
LOCAL_CI_HOME = os.path.expanduser(os.environ.get("LOCAL_CI_HOME", "~/.local-ci"))
SLACK_SCRIPTS = os.path.expanduser("~/path/to/slack-plugin/scripts")
UV = os.path.expanduser("~/.local/bin/uv")
ACCEL_DIR = os.path.expanduser("~/path/to/intake-db")
WEEKLY_PLANS = os.path.expanduser("~/path/to/weekly-plans")
CLAUDE_HISTORY = os.path.expanduser("~/.claude/history.jsonl")
CLAUDE_CWD = os.path.expanduser("~/work")
# Tools a nested item session may use without a prompt. Read-only + Shipyard task writes + Slack read.
CLAUDE_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash(gh:*)", "Bash(git log:*)", "Bash(git diff:*)",
    "mcp__shipyard__create_task", "mcp__shipyard__get_task", "mcp__shipyard__update_task", "mcp__shipyard__add_task_note",
    "mcp__shipyard__list_tasks", "mcp__shipyard__create_document", "mcp__shipyard__update_document",
    "mcp__knowledge-base__search_knowledge", "mcp__knowledge-base__add_knowledge", "mcp__knowledge-base__ingest_to_graph",
    "mcp__claude_ai_Slack__slack_read_thread", "mcp__claude_ai_Slack__slack_search_public"]

# Hosts and orgs the app builds links for and matches against.
SHIPYARD_HOST = "tickets.example.com"
RADAR_HOST = "dashboards.example.com"
SLACK_HOST = "example.slack.com"
TEAM_NAME = "Your Team"

# The EPD Rolling Roadmap board (a Shipyard HTML doc) and the CLI that is the only way to change it
EPD_BOARD_URL = "https://tickets.example.com/docs/your-board-doc-id/render"
EPD_ARTIFACT_URL = EPD_BOARD_URL
ROADMAP_CLI = os.path.expanduser("~/work/agent-system/playbooks/epd-rolling-roadmap/roadmap.py")

# --- Make it yours. Everything below is the part of the app that was specific to the author's setup: the ticket
# prefix the UI pins, the exact text the PRD / Done / Monitor buttons type into a Claude pane and the first message
# a new session gets, the calendar command behind the checkpoint strip, and the LaunchDarkly tag the Team audit reads.
# Put your own skill names in the command strings, or leave them as plain English and let Claude work it out.
TICKET_PREFIX = "PROJ"                 # keys look like PREFIX-123; the UI pins any PREFIX-\d+ it sees in a session
TICKET_SYSTEM_NAME = "ticket"          # how the prompts refer to your ticket system ("Shipyard", "Jira", "Linear"...)
TIMEZONE = "America/Denver"
# Command that prints today's checkpoint calendar events, one per line: "<KEY> <title> <google-calendar-url>".
# None disables the checkpoint strip.
CHECKPOINTS_CMD = None
LD_PROJECT = "default"; LD_ENV = "production"; LD_TAG = "your-team-tag"   # Team audit flag check; token from LD_API_KEY or ~/.ld_env; skipped without one
# What the buttons type into the session. {title}, {key}, {key_note}, {url}, {prefix}, {team_name}, {team_id} are filled in.
PRD_COMMAND = "Write a short spec for: {title}{key_note}. Save it where specs live and reply with the link or path on its own line."
DONE_COMMAND = "Wrap this up: say what was done, what is left, and where the artifacts are."
PR_MONITOR_COMMAND = ("Monitor this PR: {url}. Check CI status, review threads and mergeability, fix what you can, "
                      "and tell me when it is ready to merge or needs me.")
# The instruction at the end of a new session's first message, when the item has no ticket yet / already has one.
SEED_ASK_NEW = ("Give me a three-sentence read: what this is, what it would take, and what you would do first. "
                "Then go do the work the note asks for. Stop and ask only if the work is destructive, outward-facing, "
                "or the note is genuinely ambiguous about what I want.")
SEED_ASK_TICKET = ("This is {key}. Read it, then give me a three-sentence read on where it stands and the next step, "
                   "then go do that next step. Stop and ask only if the work is destructive, outward-facing, or genuinely ambiguous.")
