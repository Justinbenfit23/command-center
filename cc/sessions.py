"""Claude Code sessions from ~/.claude/history.jsonl, with liveness from tmux pane titles + activity.md."""
import json, os, re, subprocess, glob
from datetime import datetime
from .config import CLAUDE_HISTORY, WEEKLY_PLANS, CLAUDE_CWD

def _tmux_titles():
    try:
        out = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{session_name}:#{window_index}.#{pane_index}\t#{pane_title}"],
                             capture_output=True, text=True, timeout=5).stdout
        return [l.split("\t", 1) for l in out.splitlines() if "\t" in l]
    except Exception:
        return []

def _active_ids(hours=4):
    """session id prefixes whose stop-hook row in the newest activity.md is within `hours`."""
    files = sorted(glob.glob(os.path.join(WEEKLY_PLANS, "week_*", "activity.md")))
    if not files: return {}
    ids, now = {}, datetime.now()
    for line in open(files[-1]):
        m = re.match(r"\| `([0-9a-f]{6,})` \| ([\w-]+) \| ([^|]*) \| ([^|]*) \|", line)
        if not m: continue
        try: last = datetime.strptime(m.group(4).strip(), "%Y-%m-%d %H:%M")
        except ValueError: continue
        if (now - last).total_seconds() <= hours * 3600:
            ids[m.group(1)] = {"tool": m.group(2), "last": m.group(4).strip()}
    return ids

def _jsonl_mtime(sid):
    """newest mtime of this session's transcript across project dirs; Claude appends per turn."""
    best = 0
    for f in glob.glob(os.path.expanduser(f"~/.claude/projects/*/{sid}.jsonl")):
        try: best = max(best, os.path.getmtime(f))
        except OSError: pass
    return best

def list_sessions(limit=40):
    if not os.path.exists(CLAUDE_HISTORY): return []
    first, last, proj = {}, {}, {}
    for line in open(CLAUDE_HISTORY, errors="replace"):
        try: d = json.loads(line)
        except Exception: continue
        sid = d.get("sessionId");
        if not sid: continue
        disp = (d.get("display") or "").strip()
        ts = d.get("timestamp") or 0
        proj[sid] = d.get("project") or proj.get(sid)
        if sid not in first and disp and not disp.startswith("/") and not disp.startswith("["):
            first[sid] = disp
        last[sid] = max(last.get(sid, 0), ts)
    active = _active_ids(); now = datetime.now().timestamp()
    out = []
    for sid, ts in sorted(last.items(), key=lambda x: -x[1])[:limit]:
        title = first.get(sid) or "(untitled)"
        title = re.sub(r"^\[Pasted text[^\]]*\]\s*", "", title)
        title = re.sub(r"\s+", " ", title).strip()[:90]
        mt = _jsonl_mtime(sid)
        live = (now - mt) <= 30 * 60 if mt else False
        out.append({"id": sid, "title": title, "project": proj.get(sid),
                    "last_active": datetime.fromtimestamp(max(ts / 1000, mt)).isoformat(timespec="minutes"),
                    "live": live or sid[:8] in active, "pane": None, "activity": active.get(sid[:8])})
    return sorted(out, key=lambda x: x["last_active"], reverse=True)

def open_in_terminal(sid, project):
    cmd = f'cd "{project or CLAUDE_CWD}" && claude --resume {sid}'
    script = f'tell application "Terminal" to do script "{cmd}"\ntell application "Terminal" to activate'
    subprocess.Popen(["osascript", "-e", script]); return cmd
