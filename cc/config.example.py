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

# The handoff DM. Fixed wording so every engineer gets the same ask.
HANDOFF_TEMPLATE = (
    "Assigning you {title}.\n\n{links}\n\n"
    "If you don't have the bandwidth for this let me know. Otherwise please confirm. Thanks"
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

# The claude.ai artifact the EPD Rolling Roadmap lives in (Team tab mirrors it)
EPD_ARTIFACT_URL = "https://claude.ai/artifact/your-roadmap-artifact-id"
