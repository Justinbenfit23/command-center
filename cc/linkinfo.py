"""Short descriptions for the extra links pinned on a card (items.links): who and what a Slack thread is,
a ticket's title and status, a document's title. Filled once per link, stored back in the links JSON, and
served to the slot dropdowns so nobody has to click a link to find out what it is."""
import json, re, threading, time
from . import db, slack, shipyard
from .config import SHIPYARD_HOST

DOC_ID = re.compile(rf"https://{re.escape(SHIPYARD_HOST)}/d/([0-9a-f-]{{36}})")
TICKET = re.compile(rf"https://{re.escape(SHIPYARD_HOST)}/t/([A-Z]+-\d+)")
PERIOD = 30; PER_PASS = 4; GAP = 1.2; BACKOFF = 300   # Slack's Web API 429s fast: pace the reads, back off on a miss
_lock = threading.Lock()

def describe(link):
    """One read per link. Returns the fields to merge in; `at` marks it done (also set on failure, so a dead
    link is not re-read every pass; `err` says why)."""
    kind, url = link.get("kind"), link.get("url", "")
    try:
        if kind == "slack":
            m = slack.fetch(url) or {}
            if "text" not in m:   # fetch swallows its own errors (rate limit, no access) and hands back the bare parse
                n = int(link.get("tries") or 0) + 1   # never final: a 429 burst must not brand a thread unreadable
                return {"tries": n, "retry_at": time.time() + BACKOFF * n, "err": "not read yet (retrying)"}
            return {"who": m.get("sender") or "", "what": (m.get("text") or "")[:140], "channel": m.get("channel_name") or "",
                    "ckind": m.get("channel_kind") or "", "replies": m.get("replies"), "at": time.time(), "err": None, "retry_at": None}
        if kind == "ticket":
            k = TICKET.search(url); t = shipyard.get_task(k.group(1)) if k else None
            if not t or t.get("error"): return {"err": "not found", "at": time.time()}
            return {"title": t.get("title") or "", "status": t.get("status") or "", "who": (t.get("assignee") or {}).get("display_name") or "",
                    "at": time.time()}
        if kind == "doc":
            m = DOC_ID.search(url); d = shipyard.call("get_document", document_id=m.group(1)) if m else None
            if not d or d.get("error"): return {"err": "not found", "at": time.time()}
            return {"title": d.get("title") or "", "status": d.get("status") or "", "at": time.time()}
        return {"at": time.time()}
    except Exception as e:
        return {"err": str(e)[:120], "at": time.time()}

def fill(item, limit=PER_PASS):
    """Describe up to `limit` undescribed links on this item and save. Returns the full links list."""
    links = item.get("links") or []
    if isinstance(links, str):
        try: links = json.loads(links)
        except Exception: links = []
    n = 0
    for l in links:
        if n >= limit: break
        if l.get("kind") in ("slack", "ticket", "doc") and not l.get("at") and (l.get("retry_at") or 0) <= time.time():
            if n: time.sleep(GAP)
            l.update(describe(l)); n += 1
    if n:
        # re-read before writing: pin_links may have appended a link while we were reading
        cur = db.get_item(item["id"]) or {}
        have = {l["url"]: l for l in (cur.get("links") or [])}
        for l in links: have[l["url"]] = {**have.get(l["url"], {}), **l}
        db.update_item(item["id"], links=json.dumps(list(have.values())))
        return list(have.values())
    return links

def _loop():
    while True:
        try:
            budget = 10   # reads per pass across all cards
            for it in db.list_items("open"):
                if budget <= 0: break
                todo = sum(1 for l in (it.get("links") or []) if l.get("kind") in ("slack", "ticket", "doc") and not l.get("at") and (l.get("retry_at") or 0) <= time.time())
                if todo:
                    fill(it, limit=min(PER_PASS, budget)); budget -= min(todo, PER_PASS); time.sleep(GAP)
        except Exception as e:
            print(f"linkinfo: {e}", flush=True)
        time.sleep(PERIOD)

def start():
    threading.Thread(target=_loop, daemon=True).start()
