"""The EPD Rolling Roadmap, as the team tab sees it.

Since GTMP-1181 the roadmap is one HTML file mastered in Shipyard, and roadmap.py (the CLI embedded in
that file) is the only thing that changes it. This module shells out to that CLI: `rows()` reads the
working copy, `set_field()` runs one op with --push, `sync_all()` runs `sync --owner` for every owner
and pushes once. No local mirror, no reconcile, no Claude in the loop.
"""
import json, os, re, subprocess, sys
from .config import TEAM, ME, ME_UID, ROADMAP_CLI, EPD_BOARD_URL

SLUG_BY_LOGIN = {login: login.split("-")[0] for login in TEAM}
SLUG_BY_LOGIN[ME] = "justin"
PHASES = ("Discovery & Definition", "Design Production", "Technical Discovery", "Development", "Evaluation", "Done", "On Hold")
DELIVERY = ("Not Started", "On Time", "Delayed", "On Hold", "Punted")
WORKING = os.path.join(os.path.dirname(ROADMAP_CLI), "epd-rolling-roadmaps.html")
_END = "</scr" + "ipt>"
_DATA = re.compile(r'<script type="application/json" id="roadmap-data">\n(.*?)\n' + _END, re.S)


def cli(*args, by="justin", timeout=180):
    r = subprocess.run([sys.executable, ROADMAP_CLI, "--by", by, *args], capture_output=True, text=True, timeout=timeout)
    out = (r.stdout + r.stderr).strip()
    if r.returncode:
        raise RuntimeError(out[-400:] or f"roadmap.py exit {r.returncode}")
    return out


def board():
    """The data block of the working copy (whatever the last pull or push left there)."""
    if not os.path.exists(WORKING):
        cli("pull")
    m = _DATA.search(open(WORKING, encoding="utf-8").read())
    return json.loads(m.group(1)) if m else {"projects": [], "revision": None}


def pull():
    return cli("pull")


def rows(team_view_only=False):
    out = []
    for t in board().get("projects", []):
        if t.get("phase") == "Done" or (team_view_only and not t.get("team_view")):
            continue
        out.append({**t, "task_key": t["id"], "ticket": (t.get("tickets") or [None])[0]})
    return out


def top_three():
    by = {}
    for r in rows(team_view_only=True):
        by.setdefault(r.get("assignee") or "unassigned", []).append(r)
    return {k: sorted(v, key=lambda r: r.get("rank") or 99)[:3] for k, v in by.items()}


def set_field(pid, field, value, by="justin"):
    """One edit from the team tab, straight onto the master."""
    if field == "team_view":
        return cli("top3", pid, *([] if value else ["--off"]), "--push", by=by)
    if field == "rank":
        return cli("top3", pid, *(["--rank", str(int(value))] if value else ["--off"]), "--push", by=by)
    return cli("set", pid, f"{field}={'' if value is None else value}", "--push", by=by)


def sync_all():
    """Every owner's Shipyard work onto the board, then one push. Deterministic; safe to run often."""
    cli("pull")
    lines = []
    for slug in sorted(set(SLUG_BY_LOGIN.values())):
        try:
            lines.append(cli("sync", "--owner", slug).splitlines()[0])
        except Exception as e:
            lines.append(f"sync {slug}: failed {str(e)[:120]}")
    lines.append(cli("push").splitlines()[0])
    return "; ".join(lines)
