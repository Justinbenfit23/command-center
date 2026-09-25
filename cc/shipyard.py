"""Shipyard via its stdio MCP server (`shipyard-mcp-stdio`), spoken directly over JSON-RPC.
One long-lived process, one lock. Same tools Claude uses, no Claude in the loop."""
import json, subprocess, threading, itertools, time
from .config import TEAM_ID, ME_UID

_proc = None; _lock = threading.Lock(); _ids = itertools.count(1)

def _start():
    global _proc
    _proc = subprocess.Popen(["shipyard-mcp-stdio"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    _rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "command-center", "version": "2"}})
    _proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"); _proc.stdin.flush()

def _rpc(method, params=None):
    i = next(_ids)
    _proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n"); _proc.stdin.flush()
    deadline = time.time() + 60
    while time.time() < deadline:
        line = _proc.stdout.readline()
        if not line: raise RuntimeError("shipyard mcp closed")
        try: d = json.loads(line)
        except Exception: continue
        if d.get("id") == i:
            if "error" in d: raise RuntimeError(str(d["error"])[:300])
            return d["result"]
    raise TimeoutError(method)

def call(tool, **args):
    with _lock:
        if _proc is None or _proc.poll() is not None: _start()
        try: r = _rpc("tools/call", {"name": tool, "arguments": args})
        except (BrokenPipeError, RuntimeError):
            _start(); r = _rpc("tools/call", {"name": tool, "arguments": args})
    txt = r["content"][0]["text"] if r.get("content") else "{}"
    try: return json.loads(txt)
    except Exception: return {"raw": txt}

OPEN = ("backlog", "todo", "in_progress", "in_review")

def open_tasks(assignee_uid=None, reporter_uid=None, limit=100):
    args = {"team_id": TEAM_ID, "limit": limit}
    if assignee_uid: args["assignee_id"] = assignee_uid
    if reporter_uid: args["reporter_id"] = reporter_uid
    d = call("list_tasks", **args)
    return [t for t in d.get("tasks", []) if t.get("status") in OPEN]

def get_task(key): return call("get_task", task_id=key)

def create_task(title, description, priority="medium", assignee_uid=None):
    args = {"team_id": TEAM_ID, "title": title[:200], "description": description, "priority": priority}
    if assignee_uid: args["assignee_id"] = assignee_uid
    return call("create_task", **args)

def add_note(key, content): return call("add_task_note", task_id=key, content=content)

PROGRESS = {"backlog": 0, "todo": 10, "in_progress": 50, "in_review": 85, "done": 100}

import re as _re
_DOC = None
_prd_cache = {}   # key -> (updated_at, prd_url)

def prd_url(task):
    """The PRD behind a task: metadata.plan (set by the PRD button / handoff), else the first Shipyard document
    link in the description. One get_task per task, re-fetched only when the task's updated_at moves."""
    global _DOC
    key = task.get("key")
    if not key: return None
    hit = _prd_cache.get(key)
    if hit and hit[0] == task.get("updated_at"): return hit[1]
    try: full = call("get_task", task_id=key, include=[])
    except Exception: return hit[1] if hit else None
    if _DOC is None:
        from .config import SHIPYARD_HOST
        _DOC = _re.compile(rf"https://{_re.escape(SHIPYARD_HOST)}/d/[0-9a-f-]{{36}}")
    url = ((full.get("metadata") or {}).get("plan") or "").strip() or None
    if not url:
        m = _DOC.search(full.get("description") or ""); url = m.group(0) if m else None
    _prd_cache[key] = (task.get("updated_at"), url)
    return url

def enrich(task):
    """Complexity + progress from what the list row carries; description length needs a get_task so it is optional."""
    p = task.get("priority") or "medium"
    base = {"low": 1, "medium": 2, "high": 3, "urgent": 3}.get(p, 2)
    desc = len(task.get("description") or "")
    size = "S" if base == 1 and desc < 800 else "L" if base == 3 or desc > 2500 else "M"
    task["complexity"] = size; task["progress"] = PROGRESS.get(task.get("status"), 0)
    # We work at the PRD level: the row's link is the PRD when the task has one, the ticket only as a fallback.
    task["prd_url"] = prd_url(task); task["ticket_url"] = task.get("url")
    task["url"] = task["prd_url"] or task.get("url")
    return task
