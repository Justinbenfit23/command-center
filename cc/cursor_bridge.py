"""Bridge between the Command Center and a Cursor agent session.

Cursor-model items don't use tmux. Messages flow through the filesystem:

    ~/.command-center/cursor-bridge/
        heartbeat                     # Cursor loop writes a unix timestamp every tick
        <item_id>/
            meta.json                 # item context (title, note, shipyard_key, etc.)
            inbox.jsonl               # CC appends user messages; Cursor reads + truncates
            outbox.jsonl              # full transcript (user + assistant turns)
            status.json               # {status, last_tool, updated_at}

The CC polls outbox.jsonl for the chat panel and status.json for the status dot.
"""
import json, os, time

BRIDGE = os.path.expanduser("~/.command-center/cursor-bridge")


def _dir(item_id):
    d = os.path.join(BRIDGE, str(item_id))
    os.makedirs(d, exist_ok=True)
    return d


# ── CC side: write ──────────────────────────────────────────────────────────

def send(item_id, text, meta=None):
    """Append a user message to the inbox (for Cursor) and outbox (for the chat panel)."""
    d = _dir(item_id)
    ts = time.time()
    with open(os.path.join(d, "inbox.jsonl"), "a") as f:
        f.write(json.dumps({"role": "user", "text": text, "ts": ts, "processed": False}, default=str) + "\n")
    with open(os.path.join(d, "outbox.jsonl"), "a") as f:
        f.write(json.dumps({"role": "user", "text": text, "ts": ts}, default=str) + "\n")
    if meta:
        write_meta(item_id, meta)


def write_meta(item_id, meta):
    d = _dir(item_id)
    tmp = os.path.join(d, ".meta.tmp")
    with open(tmp, "w") as f:
        json.dump(meta, f, default=str, ensure_ascii=False)
    os.replace(tmp, os.path.join(d, "meta.json"))


# ── Cursor side: read inbox, write outbox ───────────────────────────────────

def pending(item_id):
    """Unprocessed inbox messages for one item."""
    p = os.path.join(_dir(item_id), "inbox.jsonl")
    if not os.path.exists(p):
        return []
    msgs = []
    with open(p, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    return msgs


def all_pending():
    """Item ids with unprocessed inbox messages, sorted oldest first."""
    if not os.path.isdir(BRIDGE):
        return []
    out = []
    for name in os.listdir(BRIDGE):
        if name == "heartbeat":
            continue
        p = os.path.join(BRIDGE, name, "inbox.jsonl")
        if os.path.exists(p) and os.path.getsize(p) > 0:
            out.append((name, os.path.getmtime(p)))
    return [item_id for item_id, _ in sorted(out, key=lambda x: x[1])]


def clear_inbox(item_id):
    p = os.path.join(_dir(item_id), "inbox.jsonl")
    if os.path.exists(p):
        os.remove(p)


def reply(item_id, text):
    """Cursor writes an assistant response to the outbox."""
    d = _dir(item_id)
    with open(os.path.join(d, "outbox.jsonl"), "a") as f:
        f.write(json.dumps({"role": "assistant", "text": text, "ts": time.time()}, default=str) + "\n")


def set_status(item_id, status, last_tool=None):
    d = _dir(item_id)
    tmp = os.path.join(d, ".status.tmp")
    with open(tmp, "w") as f:
        json.dump({"status": status, "last_tool": last_tool, "updated_at": time.time()}, f)
    os.replace(tmp, os.path.join(d, "status.json"))


def read_meta(item_id):
    p = os.path.join(_dir(item_id), "meta.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


# ── CC side: read ───────────────────────────────────────────────────────────

def transcript(item_id, limit=14):
    """Read the outbox as a transcript (same shape as live.transcript)."""
    p = os.path.join(_dir(item_id), "outbox.jsonl")
    if not os.path.exists(p):
        return []
    msgs = []
    with open(p, errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msgs.append(json.loads(line))
            except Exception:
                pass
    return msgs[-limit:]


def get_status(item_id):
    """Status of this item's bridge session. Factors in the heartbeat to detect a dead loop."""
    d = _dir(item_id)
    # Check heartbeat first
    alive = is_alive()
    # Item-level status
    sp = os.path.join(d, "status.json")
    if os.path.exists(sp):
        try:
            with open(sp) as f:
                st = json.load(f)
        except Exception:
            st = {}
    else:
        st = {}
    base = st.get("status", "idle")
    # If inbox has messages, Cursor hasn't processed them yet
    ip = os.path.join(d, "inbox.jsonl")
    has_pending = os.path.exists(ip) and os.path.getsize(ip) > 0
    if has_pending and base != "busy":
        base = "busy" if alive else "idle"
    if not alive and base == "busy":
        base = "idle"
    return {"status": base, "last_tool": st.get("last_tool"),
            "cursor_alive": alive, "has_pending": has_pending}


# ── Heartbeat ───────────────────────────────────────────────────────────────

def heartbeat():
    """Cursor loop writes this every tick so the CC knows the loop is alive."""
    os.makedirs(BRIDGE, exist_ok=True)
    p = os.path.join(BRIDGE, "heartbeat")
    with open(p, "w") as f:
        f.write(str(time.time()))


def is_alive(max_age=30):
    """True if the Cursor loop has ticked in the last max_age seconds."""
    p = os.path.join(BRIDGE, "heartbeat")
    if not os.path.exists(p):
        return False
    try:
        with open(p) as f:
            ts = float(f.read().strip())
        return (time.time() - ts) < max_age
    except Exception:
        return False


# ── Cleanup ─────────────────────────────────────────────────────────────────

def clear(item_id):
    """Remove all bridge state for an item."""
    import shutil
    d = os.path.join(BRIDGE, str(item_id))
    if os.path.isdir(d):
        shutil.rmtree(d, ignore_errors=True)
