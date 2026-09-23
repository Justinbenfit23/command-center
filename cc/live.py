"""Live interactive Claude Code sessions, from ~/.claude/sessions/<pid>.json (pid, sessionId, tmux pane, status, cwd).
Read = the session's transcript jsonl. Write = `tmux send-keys` into its pane, so text lands in the real conversation."""
import glob, json, os, subprocess, time

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

def _title(pane):
    t = _tmux("display", "-t", pane, "-p", "#{pane_title}") if pane else ""
    return t.lstrip("✳ ").strip() or None

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

def screen(pane, lines=40):
    return _tmux("capture-pane", "-p", "-t", pane, "-S", f"-{lines}") if pane else ""

def list_live():
    out = []
    for f in glob.glob(SESS):
        try: d = json.load(open(f))
        except Exception: continue
        if not _alive(d.get("pid")): continue
        pane = _pane(d.get("tmux") or "")
        out.append({"sid": d["sessionId"], "pid": d["pid"], "pane": pane, "kind": d.get("kind"), "status": d.get("status"),
                    "title": _title(pane) or d.get("name"), "name": d.get("name"), "cwd": d.get("cwd"),
                    "updated_at": d.get("updatedAt"), "started_at": d.get("startedAt")})
    order = {"busy": 0, "idle": 1, "shell": 2}
    return sorted(out, key=lambda s: (order.get(s["status"], 3), -(s["updated_at"] or 0)))

def send(pane, text):
    """Type into the live session and press Enter. -l sends the text literally (no key-name parsing)."""
    if not pane: raise RuntimeError("session has no tmux pane")
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

def spawn(cwd=None, timeout=60, model=None):
    """Open a new tmux window running `claude`; wait for it to register. Returns (sid or None, pane).
    A None sid means it is still booting (slow SessionStart hooks); the caller keeps the pane and finishes later."""
    host = _host_session(); cwd = cwd or os.path.expanduser("~/work")
    args = ["new-window", "-d", "-P", "-F", "#{pane_id}", "-c", cwd, "claude" + (f" --model {model}" if model else "")]
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
