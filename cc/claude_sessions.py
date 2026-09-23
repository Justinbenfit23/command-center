"""One nested Claude Code session per intake item, driven with `claude -p`.
First turn creates the session (--session-id), later turns --resume it. The same id works in a terminal
(`claude --resume <id>`), so nothing is trapped in this UI. Replies are scanned for a GTMP key and it is
written back onto the item, which is how a ticket "auto-populates"."""
import json, re, subprocess, uuid
from .config import CLAUDE_CWD, CLAUDE_ALLOWED_TOOLS, GH_ORG, SHIPYARD_HOST, RADAR_HOST, SLACK_HOST, TEAM_ID, TEAM_NAME
from . import db

KEY = re.compile(r"\bGTMP-\d{2,5}\b")
DOC = re.compile(rf"https://{re.escape(SHIPYARD_HOST)}/d/[0-9a-f-]{{36}}")
PR  = re.compile(rf"https://github\.com/{re.escape(GH_ORG)}/[\w.-]+/pull/\d+")
RADAR = re.compile(rf"https://{re.escape(RADAR_HOST)}/[\w/?=&.-]+")
SLACKL = re.compile(rf"https://{re.escape(SLACK_HOST)}/archives/[A-Z0-9]+/p\d{{16}}(?:\?thread_ts=[\d.]+)?")

def pin_links(item, text):
    """Anything a session produces that has a home on the row gets pinned there. First key/doc wins;
    PRs, Radar links and Slack links accumulate (deduped) in the item's `links` JSON."""
    upd = {}
    m = KEY.search(text)
    if m and not item.get("shipyard_key"): upd["shipyard_key"] = m.group(0)
    d = DOC.search(text)
    if d and not item.get("prd_url"): upd["prd_url"] = d.group(0)
    links = json.loads(item.get("links") or "[]") if isinstance(item.get("links"), str) else (item.get("links") or [])
    have = {l["url"] for l in links}
    for kind, rx in (("pr", PR), ("radar", RADAR), ("slack", SLACKL)):
        for u in rx.findall(text):
            if u not in have: links.append({"kind": kind, "url": u}); have.add(u)
    if len(links) != len(json.loads(item.get("links") or "[]") if isinstance(item.get("links"), str) else (item.get("links") or [])): upd["links"] = json.dumps(links)
    if upd: db.update_item(item["id"], **upd)
    return upd

def seed_prompt(item):
    src = f"Slack link: {item['permalink']}\n" if item.get("permalink") else ""
    req = f"Requester: {item.get('requester')} (tier {item.get('vip_tier')}), channel #{item.get('channel')}\n" if item.get("requester") else ""
    return (f"Intake item from the command center.\n{src}{req}Ask: {item['title']}\n"
            + (f"First-look note: {item['note']}\n" if item.get("note") else "")
            + (f"Message text: {item['body'][:1500]}\n" if item.get("body") else "")
            + f"\nDo two things. (1) Create a Shipyard task on team {TEAM_NAME} (team_id {TEAM_ID}) "
              "titled from the ask, description carrying the requester, link, note and what is actually being asked; reply with its key "
              "on its own line as `Ticket: KEY-NNN`. (2) Then give a three-sentence read: what this is, what it would take "
              "(simple build / Snowflake model / opp-gen or Pulse PR / UI change / feature / analysis / Radar dashboard / quick answer), "
              "and whether it is his or the team's. Do not start building.")

def send(item, text):
    """Run one turn. Returns assistant text. Persists both sides. Creates the session on first use."""
    sid = item.get("claude_session_id"); first = not sid
    if first: sid = str(uuid.uuid4())
    cmd = ["claude", "-p", text, "--output-format", "json", "--allowedTools", *CLAUDE_ALLOWED_TOOLS]
    cmd += ["--session-id", sid] if first else ["--resume", sid]
    db.add_message(item["id"], "user", text)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=CLAUDE_CWD)
    out = r.stdout.strip()
    try: reply = json.loads(out).get("result") or out
    except Exception: reply = out or r.stderr.strip()[:800] or "(no reply)"
    if first: db.update_item(item["id"], claude_session_id=sid)
    db.add_message(item["id"], "assistant", reply)
    pin_links(db.get_item(item["id"]), reply)
    return reply

def ask(item):     return send(item, seed_prompt(item) if not item.get("claude_session_id") else "Give me your read on this one: what is it, what would it take, mine or the team's?")
def prd(item):
    p = ("/prd-authoring " + item["title"] + (f" (Shipyard {item['shipyard_key']})" if item.get("shipyard_key") else "")
         + ". Publish to Shipyard and reply with the document URL on its own line.")
    return send(item, (seed_prompt(item) + "\n\nThen: " + p) if not item.get("claude_session_id") else p)
def dbr_done(item):
    return send(item, "run dbr done") if item.get("claude_session_id") else None
