"""Live interactive Claude Code sessions, from ~/.claude/sessions/<pid>.json (pid, sessionId, tmux pane, status, cwd).
Read = the session's transcript jsonl. Write = `tmux send-keys` into its pane, so text lands in the real conversation."""
import glob, json, os, subprocess, time, re
from . import db

SESS = os.path.expanduser("~/.claude/sessions/*.json")
PROJ = os.path.expanduser("~/.claude/projects")

def _alive(pid):
    try: os.kill(int(pid), 0); return True
    except Exception: return False

def _pane(target):
    """'sep142026:@0.%12' -> '%12' (tmux accepts the bare pane id anywhere)."""
    return target.rsplit(".", 1)[-1] if target and "%" in target else None

def _tmux(*args, timeout=5):
    try: return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=timeout).stdout.rstrip("\n")
    except Exception: return ""

def _titles():
    """Every pane title in ONE tmux call. The old per-pane `display` cost ~50ms each, and with a dozen
    live sessions every poll paid it a dozen times."""
    out = {}
    for line in _tmux("list-panes", "-a", "-F", "#{pane_id}\t#{pane_title}").splitlines():
        if "\t" in line:
            pid, t = line.split("\t", 1); out[pid] = t.lstrip("✳ ").strip() or None
    return out

def _title(pane, titles=None):
    if not pane: return None
    if titles is None: titles = _titles()
    return titles.get(pane)

def _transcript_path(sid):
    hits = glob.glob(os.path.join(PROJ, "*", f"{sid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None

def _text_of(content):
    """Human-readable text from a message content (str or block list). Tool noise collapsed to one line each."""
    if isinstance(content, str): return content
    out = []
    for b in content or []:
        t = b.get("type")
        if t == "text" and b.get("text", "").strip(): out.append(b["text"].strip())
        elif t == "tool_use": out.append(f"⚙ {b.get('name')}" + (f": {str(b.get('input', {}).get('description') or b.get('input', {}).get('command') or '')[:80]}" if b.get("input") else ""))
        elif t == "tool_result": pass   # the assistant's next turn says what it learned
        elif t == "thinking": pass
    return "\n".join(out)

def transcript(sid, limit=14, include_tools=False):
    p = _transcript_path(sid)
    if not p: return []
    lines = open(p, errors="replace").readlines()[-400:]
    msgs = []
    for l in lines:
        try: d = json.loads(l)
        except Exception: continue
        if d.get("type") not in ("user", "assistant") or d.get("isMeta"): continue
        txt = _text_of((d.get("message") or {}).get("content"))
        if not include_tools: txt = "\n".join(l for l in txt.splitlines() if not l.startswith("⚙ "))
        if not txt.strip(): continue
        if msgs and msgs[-1]["role"] == d["type"] and d["type"] == "assistant": msgs[-1]["text"] += "\n" + txt; continue
        msgs.append({"role": d["type"], "text": txt[:4000], "ts": d.get("timestamp")})
    return msgs[-limit:]

def pending_question(sid):
    """An AskUserQuestion the session is blocked on. While its dialog is up in the terminal, anything typed
    from here lands in the dialog, and Enter picks the highlighted answer, so a chat message sent from the
    card silently answers the question with the defaults instead of reaching Claude."""
    p = _transcript_path(sid)
    if not p: return None
    asked = {}
    for l in open(p, errors="replace").readlines()[-300:]:
        try: d = json.loads(l)
        except Exception: continue
        c = (d.get("message") or {}).get("content")
        if not isinstance(c, list): continue
        for b in c:
            if not isinstance(b, dict): continue
            if b.get("type") == "tool_use" and b.get("name") == "AskUserQuestion":
                asked[b.get("id")] = [{"q": q.get("question"), "options": [o.get("label") for o in q.get("options") or []]}
                                      for q in (b.get("input") or {}).get("questions") or []]
            elif b.get("type") == "tool_result": asked.pop(b.get("tool_use_id"), None)
    return list(asked.values())[-1] if asked else None

_DIALOG = re.compile(r"^\s*❯\s+\d+\.\s+\S")
def dialog(pane):
    """A modal in the terminal (permission prompt, folder trust, model switch): a numbered option list with
    the ❯ cursor on one of them. Keys sent from here go into it, not the prompt box."""
    if not pane: return None
    tail = [l.rstrip() for l in screen(pane, 25).splitlines()[-25:]]
    if not any(_DIALOG.match(l) for l in tail): return None
    if not any(("esc" in l.lower() or "enter" in l.lower()) for l in tail): return None
    body = [l for l in tail if l.strip()]
    return "\n".join(body[-12:])

def screen(pane, lines=40):
    return _tmux("capture-pane", "-p", "-t", pane, "-S", f"-{lines}") if pane else ""

_cache = {"at": 0.0, "val": [], "roster_at": 0.0}
_CACHE_TTL = 2.0
_ROSTER_EVERY = 15.0

def server_up():
    return subprocess.run(["tmux", "has-session"], capture_output=True, timeout=5).returncode == 0

HOST_NAME = "cc"

def ensure_host(cwd=None):
    """The tmux server the sessions live in. Started detached, so it belongs to no Terminal window: closing or
    killing Terminal.app leaves it (and every Claude inside it) running. Returns the host session name."""
    if server_up(): return _host_session() or HOST_NAME
    subprocess.run(["tmux", "new-session", "-d", "-s", HOST_NAME, "-c", cwd or os.path.expanduser("~/work")], check=True, timeout=10)
    return HOST_NAME

def transcript_cwd(sid):
    """The cwd a session ran in, from its transcript. `claude --resume` must run there or it cannot find the session."""
    p = _transcript_path(sid)
    if not p: return None
    with open(p, errors="replace") as f:
        for _ in range(40):
            l = f.readline()
            if not l: break
            m = re.search(r'"cwd":"([^"]+)"', l)
            if m: return m.group(1)
    return None

def mem():
    """Free memory the way the OOM dialog sees it, plus swap. Per-session cost is measured, not guessed: the Claude
    process is ~0.55 GB and the MCP servers each session spawns are ~1.35 GB more, so ~1.9 GB a session."""
    out = {"free_gb": None, "swap_used_gb": None, "swap_total_gb": None, "total_gb": None, "per_session_gb": 1.9}
    try:
        out["total_gb"] = round(int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=3).stdout) / 2**30, 1)
        vs = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=3).stdout
        page = int(re.search(r"page size of (\d+)", vs).group(1))
        g = lambda k: int(re.search(rf"{k}:\s+(\d+)", vs).group(1))
        out["free_gb"] = round((g("Pages free") + g("Pages inactive") + g("Pages speculative") + g("Pages purgeable")) * page / 2**30, 1)
        # memory_pressure's number is the one macOS itself acts on (the "out of application memory" dialog)
        mp = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"free percentage:\s*(\d+)%", mp)
        if m and out["total_gb"]: out["free_gb"] = round(out["total_gb"] * int(m.group(1)) / 100, 1)
        sw = subprocess.run(["sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"total = ([\d.]+)M\s+used = ([\d.]+)M", sw)
        if m: out["swap_total_gb"] = round(float(m.group(1)) / 1024, 1); out["swap_used_gb"] = round(float(m.group(2)) / 1024, 1)
    except Exception: pass
    return out

def list_live(fresh=False):
    """Live sessions, cached for 2s: the UI polls this several times a second across open rows."""
    if not fresh and time.time() - _cache["at"] < _CACHE_TTL: return list(_cache["val"])
    out = []; titles = _titles()
    for f in glob.glob(SESS):
        try: d = json.load(open(f))
        except Exception: continue
        if not _alive(d.get("pid")): continue
        pane = _pane(d.get("tmux") or "")
        out.append({"sid": d["sessionId"], "pid": d["pid"], "pane": pane, "kind": d.get("kind"), "status": d.get("status"),
                    "title": _title(pane, titles) or d.get("name"), "name": d.get("name"), "cwd": d.get("cwd"),
                    "updated_at": d.get("updatedAt"), "started_at": d.get("startedAt")})
    order = {"busy": 0, "idle": 1, "shell": 2}
    out = sorted(out, key=lambda s: (order.get(s["status"], 3), -(s["updated_at"] or 0)))
    _cache["at"] = time.time(); _cache["val"] = out
    if out and time.time() - _cache["roster_at"] > _ROSTER_EVERY:
        try: db.upsert_roster(out); _cache["roster_at"] = time.time()
        except Exception: pass
    return list(out)

def send(pane, text):
    """Type into the live session and press Enter. -l sends the text literally (no key-name parsing).

    Anything with a newline goes in as a bracketed paste instead. `send-keys -l` delivers a bare LF,
    which Claude Code reads as Enter, so a two-line message submitted itself halfway through and the
    rest arrived as a second prompt — that is what broke dropping a file alongside a typed message."""
    if not pane: raise RuntimeError("session has no tmux pane")
    if "\n" in text:
        subprocess.run(["tmux", "load-buffer", "-b", "cc_send", "-"], input=text.encode(), check=True, timeout=5)
        subprocess.run(["tmux", "paste-buffer", "-p", "-d", "-b", "cc_send", "-t", pane], check=True, timeout=5)
    else:
        subprocess.run(["tmux", "send-keys", "-t", pane, "-l", text], check=True, timeout=5)
    time.sleep(0.35)
    subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], check=True, timeout=5)

def press(pane, key="Enter"):
    subprocess.run(["tmux", "send-keys", "-t", pane, key], check=True, timeout=5)

def last_tool(sid):
    """The most recent ⚙ tool line, so the UI can say what Claude is doing while it is busy."""
    p = _transcript_path(sid)
    if not p: return None
    for l in reversed(open(p, errors="replace").readlines()[-120:]):
        try: d = json.loads(l)
        except Exception: continue
        if d.get("type") != "assistant": continue
        for b in (d.get("message") or {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                inp = b.get("input") or {}
                return f"{b.get('name')}: {str(inp.get('description') or inp.get('command') or inp.get('pattern') or inp.get('file_path') or '')[:90]}".rstrip(": ")
    return None

def focus(pane):
    if pane:
        _tmux("select-window", "-t", pane); _tmux("select-pane", "-t", pane)
    if not _tmux("list-clients").strip():
        # nothing is looking at the tmux server (Terminal was closed): open a window attached to it
        host = _host_session() or HOST_NAME
        subprocess.Popen(["osascript", "-e", f'tell application "Terminal" to do script "tmux attach -t {host}"'])
    subprocess.Popen(["osascript", "-e", 'tell application "Terminal" to activate'])


def _host_session():
    """The tmux session the Claude panes live in (e.g. 'sep142026'), from any registered session."""
    for f in glob.glob(SESS):
        try: t = json.load(open(f)).get("tmux") or ""
        except Exception: continue
        if ":" in t: return t.split(":", 1)[0]
    return _tmux("display", "-p", "#{session_name}") or None

def registered(pane):
    """(sid) if a live claude has registered this pane in ~/.claude/sessions, else None."""
    for f in glob.glob(SESS):
        try: d = json.load(open(f))
        except Exception: continue
        if _pane(d.get("tmux") or "") == pane and _alive(d.get("pid")): return d["sessionId"]
    return None

def answer_prompts(pane):
    """Claude Code asks 'Is this a project you trust?' the first time it runs in a folder. These are the operator's own
    repos under ~/work, so choose 'Yes, I trust this folder' (second option) and move on."""
    scr = screen(pane, 20)
    if "trust this folder" in scr and "Enter to confirm" in scr:
        # Enter too soon after Down lands on the highlighted "No, exit" and claude quits. Move, verify, then confirm.
        for _ in range(3):
            if "❯ Yes, I trust this folder" in screen(pane, 20): break
            subprocess.run(["tmux", "send-keys", "-t", pane, "Down"], timeout=5); time.sleep(0.7)
        if "❯ Yes, I trust this folder" in screen(pane, 20):
            subprocess.run(["tmux", "send-keys", "-t", pane, "Enter"], timeout=5); return "trusted folder"
    return None

def spawn(cwd=None, timeout=60, model=None, resume=None):
    """Open a new tmux window running `claude` (or `claude --resume <sid>`); wait for it to register.
    Returns (sid or None, pane). A None sid means it is still booting (slow SessionStart hooks); the caller
    keeps the pane and finishes later. Creates the tmux server if none is running."""
    host = ensure_host(cwd); cwd = cwd or os.path.expanduser("~/work")
    cmd = "claude" + (f" --resume {resume}" if resume else "") + (f" --model {model}" if model else "")
    args = ["new-window", "-d", "-P", "-F", "#{pane_id}", "-c", cwd, cmd]
    if host: args[1:1] = ["-t", host]
    pane = _tmux(*args).strip()
    if not pane: raise RuntimeError("tmux could not open a window")
    deadline = time.time() + timeout
    while time.time() < deadline:
        sid = registered(pane)
        if sid: return sid, pane
        answer_prompts(pane); time.sleep(0.6)
    return None, pane

def rename_pane(pane, title):
    if pane and title: _tmux("select-pane", "-t", pane, "-T", title[:60])


def _alive(pid):
    try: os.kill(int(pid), 0)
    except OSError: return False
    st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(st) and not st.startswith("Z")   # a zombie is dead, just not reaped yet

def _children(pid):
    out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True).stdout.split()
    return [int(c) for c in out if c.isdigit()]

def _kill_tree(pid):
    """SIGTERM the process and everything under it (the MCP servers), then SIGKILL whatever is still there.
    Children first, so a claude that ignores the signal cannot leave a dozen orphaned MCP servers behind."""
    kids = _children(pid)
    for k in kids: _kill_tree(k)
    for sig, grace in ((15, 2.0), (9, 0.5)):
        if not _alive(pid): return
        try: os.kill(int(pid), sig)
        except OSError: return
        end = time.time() + grace
        while time.time() < end and _alive(pid): time.sleep(0.1)

def kill(pane, pid=None):
    """End a session for real. Killing the tmux pane is the clean path (claude and its MCP servers hang up
    together), but a pane id can be stale and a claude can survive the hangup, so the pid is always checked
    afterwards and the whole tree is killed if it is still there. Errors never hide a survivor."""
    if pane: _tmux("kill-pane", "-t", pane)
    if pid:
        end = time.time() + 1.5
        while time.time() < end and _alive(pid): time.sleep(0.1)
        if _alive(pid): _kill_tree(pid)
    _cache["at"] = 0.0
    if pid and _alive(pid): raise RuntimeError(f"session pid {pid} would not die")
