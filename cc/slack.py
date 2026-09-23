"""Slack via the Web API with the keychain user token the slack plugin scripts use (no bot token).
Works the same for channels, DMs and group DMs, which the scripts' name-based reader does not."""
import re, os, sys, json, subprocess, urllib.request, urllib.parse, time
from datetime import datetime
from .config import SLACK_SCRIPTS, CHANNEL_NAMES

PERMALINK = re.compile(r"slack\.com/archives/(?P<ch>[A-Z0-9]+)/p(?P<ts>\d{16})(?:\?thread_ts=(?P<thr>[\d.]+))?")
_tok = None; _names = {}

def parse(link):
    m = PERMALINK.search(link or "")
    if not m: return None
    ts = m.group("ts"); ts = f"{ts[:10]}.{ts[10:]}"
    return {"channel_id": m.group("ch"), "ts": ts, "thread_ts": m.group("thr") or ts}

def token():
    global _tok
    if _tok: return _tok
    try:
        sys.path.insert(0, SLACK_SCRIPTS); from utils import auth  # noqa
        _tok = auth.get_user_token()
    except Exception:
        try:
            svc = re.search(r'KEYCHAIN_SERVICE_USER\s*=\s*"([^"]+)"', open(os.path.join(SLACK_SCRIPTS, "utils", "auth.py")).read()).group(1)
            _tok = subprocess.run(["security", "find-generic-password", "-s", svc, "-w"], capture_output=True, text=True, timeout=10).stdout.strip() or None
        except Exception:
            _tok = None
    return _tok

def api(method, **params):
    t = token()
    if not t: raise RuntimeError("no Slack user token in keychain (run the slack plugin auth once)")
    r = urllib.request.Request("https://slack.com/api/" + method, data=urllib.parse.urlencode(params).encode(), headers={"Authorization": "Bearer " + t})
    d = json.load(urllib.request.urlopen(r, timeout=30))
    if not d.get("ok"): raise RuntimeError(f"slack {method}: {d.get('error')}")
    return d

def name(uid):
    if not uid: return None
    if uid not in _names:
        try: u = api("users.info", user=uid)["user"]; _names[uid] = u.get("real_name") or u.get("name") or uid
        except Exception: _names[uid] = uid
    return _names[uid]

def clean(text):
    text = re.sub(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>", lambda m: "@" + (name(m.group(1)) or m.group(1)), text or "")
    text = re.sub(r"<#[A-Z0-9]+\|([^>]*)>", r"#\1", text)
    text = re.sub(r"<([^|>]+)\|([^>]*)>", r"\2 (\1)", text)
    return re.sub(r"<([^>]+)>", r"\1", text).strip()

def _msg(m):
    return {"at": datetime.fromtimestamp(float(m["ts"])).strftime("%Y-%m-%d %H:%M"), "ts": m["ts"],
            "user": name(m.get("user")) or m.get("username") or ("bot" if m.get("bot_id") else "?"),
            "text": clean(m.get("text"))[:1200], **({"replies": m.get("reply_count")} if m.get("reply_count") else {})}

def channel_info(cid):
    try:
        c = api("conversations.info", channel=cid)["channel"]
        kind = "dm" if c.get("is_im") else "group_dm" if c.get("is_mpim") else "private_channel" if c.get("is_private") else "channel"
        nm = c.get("name") or (name(c.get("user")) if c.get("is_im") else None) or CHANNEL_NAMES.get(cid) or cid
        return {"name": nm, "kind": kind}
    except Exception:
        return {"name": CHANNEL_NAMES.get(cid) or cid, "kind": "unknown"}

def fetch(link):
    """The linked message: text, sender, channel name/kind, reply count. Same shape the UI already uses."""
    p = parse(link)
    if not p: return None
    try:
        info = channel_info(p["channel_id"])
        d = api("conversations.replies", channel=p["channel_id"], ts=p["thread_ts"], limit="200")
        msgs = d.get("messages", []); root = msgs[0] if msgs else {}
        target = next((m for m in msgs if m.get("ts") == p["ts"]), root)
        p.update({"text": clean(target.get("text")), "sender": name(target.get("user")) or ("bot" if target.get("bot_id") else None),
                  "sender_id": target.get("user"), "channel_name": info["name"], "channel_kind": info["kind"], "replies": max(len(msgs) - 1, 0)})
    except Exception as e:
        print(f"slack.fetch failed: {e}", file=sys.stderr, flush=True)
    return p

def context(link, hours=24, max_msgs=120, max_chars=14000):
    """Everything said in that conversation in the `hours` before the linked message, plus the linked thread.
    This is the automatic backfill for a session seed: DM, group DM or channel, same call."""
    p = parse(link)
    if not p: return None
    ts = float(p["ts"]); info = channel_info(p["channel_id"]); out = {"channel": info["name"], "kind": info["kind"], "window_hours": hours, "before": [], "thread": []}
    try:
        h = api("conversations.history", channel=p["channel_id"], oldest=str(ts - hours * 3600), latest=str(ts), inclusive="true", limit=str(max_msgs))
        out["before"] = [_msg(m) for m in reversed(h.get("messages", [])) if m.get("subtype") not in ("channel_join", "channel_leave")]
    except Exception as e:
        out["error"] = str(e)[:200]
    try:
        r = api("conversations.replies", channel=p["channel_id"], ts=p["thread_ts"], limit="200")
        out["thread"] = [_msg(m) for m in r.get("messages", [])]
    except Exception:
        pass
    # keep the seed bounded: drop oldest "before" messages first
    while len(json.dumps(out, ensure_ascii=False)) > max_chars and out["before"]: out["before"].pop(0)
    return out


def dm(user_id, text):
    """Open (or reuse) the DM with this user and post as the authenticated user. Returns channel, ts, permalink."""
    ch = api("conversations.open", users=user_id)["channel"]["id"]
    m = api("chat.postMessage", channel=ch, text=text, unfurl_links="false")
    try: link = api("chat.getPermalink", channel=ch, message_ts=m["ts"])["permalink"]
    except Exception: link = None
    return {"channel": ch, "ts": m["ts"], "permalink": link}
