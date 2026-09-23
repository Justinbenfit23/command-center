#!/usr/bin/env python3
"""Command Center v2. Intake list with a nested Claude session per item; team activity by day + Shipyard load;
big-rocks roadmap. Stdlib only. Run: python3 server.py -> http://127.0.0.1:8765 (or `cc`)."""
import json, os, sys, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from cc import db, slack, triage, github, sessions, shipyard, roadmap, live, claude_sessions as cs
from cc.config import ME, ME_UID, TEAM, TEAM_ID, TEAM_NAME, GH_ORG, SHIPYARD_HOST, ACCEL_DIR, HANDOFF_TEMPLATE

def _intake_pg(item, meta):
    try:
        sys.path.insert(0, ACCEL_DIR); import intake_db  # noqa
        ev = intake_db.insert_event(source_type="command_center", channel_id=meta.get("channel_id"), channel_name=meta.get("channel_name"),
            sender_display_name=meta.get("sender"), sender_user_id=meta.get("sender_id"), text=item["title"], permalink=item.get("permalink"),
            thread_ts=meta.get("thread_ts"), vip_tier=item.get("vip_tier"), raw_payload={"route": item["route"], "band": item["band"]})
        if isinstance(ev, (tuple, list)): ev = ev[0] if ev else None
        if isinstance(ev, dict): ev = ev.get("id")
        return ev if isinstance(ev, int) else None
    except Exception:
        return None


def _live_index():
    return {x["sid"]: x for x in live.list_live()}

def items_with_live(status="open", sort="lift"):
    """Auto-adopt any live session that no item owns yet, then merge live status into each row."""
    idx = _live_index(); items = db.list_items(status, sort)
    if status == "open":
        owned = {it.get("live_sid") for it in items} | {it.get("live_sid") for it in db.list_items("archived")}
        for sid, x in idx.items():
            if sid in owned or x.get("kind") == "bg": continue
            db.insert_item(title=x.get("title") or x.get("name") or "Claude session", owner="me", source_type="live", live_sid=sid, route="me")
        items = db.list_items(status, sort)
    for it in items:
        x = idx.get(it.get("live_sid")) if it.get("live_sid") else None
        it["live"] = {"status": x["status"], "pane": x["pane"], "kind": x["kind"]} if x else ({"status": "booting", "pane": it["live_pane"], "kind": "interactive"} if it.get("live_pane") and it.get("pending_seed") else None)
        if x and x.get("title") and it.get("source_type") == "live" and it["title"] in (x.get("name"), "Claude session"): 
            db.update_item(it["id"], title=x["title"]); it["title"] = x["title"]
    return items

def items_with_live_one(iid):
    it = db.get_item(iid); x = _live_index().get(it.get("live_sid") or "")
    it["live"] = {"status": x["status"], "pane": x["pane"], "kind": x["kind"]} if x else None
    return it

MODELS = {"fable": "claude-fable-5-1", "opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}

def _sc(item):
    try: return json.loads(item.get("slack_context") or "null")
    except Exception: return None

def seed_for_live(item):
    """The opening message: one JSON block of everything the operator shared, then what to do with it."""
    granola = item.get("granola") or ""
    try: granola = json.loads(granola) if granola.strip().startswith(("{", "[")) else granola
    except Exception: pass
    ctx = {k: v for k, v in {
        "ask": item["title"], "requester": item.get("requester"), "requester_tier": item.get("vip_tier"),
        "channel": item.get("channel"), "slack_link": item.get("permalink"), "shipyard_ticket": item.get("shipyard_key"),
        "prd_url": item.get("prd_url"), "first_look_note": item.get("note") or None,
        "granola": granola or None, "message_text": (item.get("body") or "")[:1500] or None,
        "slack_context": _sc(item),
    }.items() if v}
    ask = (f"First, create a Shipyard task on team {TEAM_NAME} (team_id {TEAM_ID}) titled from the ask, "
           "description carrying the requester, link, note, context and what is actually being asked; reply with its key on its own line as "
           "`Ticket: GTMP-NNN`. Then give me a three-sentence read: what this is, what it would take, and whether it is mine or the team's. "
           "Do not start building until I say so." if not item.get("shipyard_key") else
           f"This is Shipyard {item['shipyard_key']}. Read it, then give me a three-sentence read on where it stands and the next step. Do not start building until I say so.")
    return ("Intake item from my command center. Everything I have is in this JSON; treat `first_look_note` as my instructions and context, "
            "`granola` as a meeting-notes link or notes, and `slack_context` as the conversation that led up to the linked message "
            "(`before` = the prior 24h in that channel/DM, `thread` = the linked thread). Read it before you answer.\n\n```json\n" + json.dumps(ctx, indent=2, ensure_ascii=False) + "\n```\n\n" + ask)

def ensure_live(item, seed=None):
    """Start a real tmux Claude session for this item if it has none. Returns the live dict.
    If claude is still booting when we give up waiting, the pane and seed are parked on the item and
    the watcher thread sends the seed the moment the session registers."""
    if item.get("live_sid") and item["live_sid"] in _live_index(): return _live_index()[item["live_sid"]]
    if item.get("live_pane") and item.get("pending_seed"): return {"sid": None, "pane": item["live_pane"], "status": "booting"}
    sid, pane = live.spawn(cwd=item.get("cwd") or None, model=MODELS.get(item.get("model") or "", item.get("model")))
    live.rename_pane(pane, item["title"])
    text = seed if seed is not None else seed_for_live(item)
    if not sid:
        db.update_item(item["id"], live_pane=pane, pending_seed=text); return {"sid": None, "pane": pane, "status": "booting"}
    db.update_item(item["id"], live_sid=sid, live_pane=pane)
    time.sleep(2.5); live.send(pane, text)
    return {"sid": sid, "pane": pane, "status": "busy"}

def _claim_after_dbr(iid, uid, wait=420):
    """dbr done often creates or closes the ticket. Once the session goes idle, pin whatever key it
    produced onto the item and hand the ticket over open rather than done."""
    end = time.time() + wait
    while time.time() < end:
        time.sleep(10)
        it = db.get_item(iid)
        if not it: return
        lv = _live_index().get(it.get("live_sid") or "")
        if lv and lv["status"] == "busy": continue
        msgs = live.transcript(it["live_sid"], limit=30) if it.get("live_sid") else []
        if msgs: cs.pin_links(it, "\n".join(m["text"] for m in msgs if m["role"] == "assistant"))
        key = (db.get_item(iid) or {}).get("shipyard_key")
        if key:
            try: shipyard.call("update_task", task_id=key, assignee_id=uid, status="todo")
            except Exception as e: print(f"post-dbr assign failed: {e}", file=sys.stderr, flush=True)
        return

def _watcher():
    """Every 2s: finish any session that registered after we stopped waiting, and answer trust prompts on the way."""
    while True:
        try:
            for it in db.list_items():
                if it.get("live_pane") and not it.get("live_sid"):
                    sid = live.registered(it["live_pane"])
                    if sid:
                        db.update_item(it["id"], live_sid=sid); time.sleep(2.5)
                        if it.get("pending_seed"): live.send(it["live_pane"], it["pending_seed"]); db.update_item(it["id"], pending_seed=None)
                    else:
                        live.answer_prompts(it["live_pane"])
        except Exception as e:
            print(f"watcher: {e}", file=sys.stderr, flush=True)
        time.sleep(2)

def team_loads():
    out = {}
    for login, t in TEAM.items():
        c = github.team_card(login)
        try: n = len(shipyard.open_tasks(assignee_uid=t[1]))
        except Exception: n = 0
        out[login] = n + c["week"] / 5
    return out

def create_item(body):
    link = (body.get("link") or "").strip(); text = (body.get("text") or "").strip(); note = (body.get("note") or "").strip()
    meta = (slack.fetch(link) if link else {}) or {}
    title = text or meta.get("text") or link
    sc = triage.score(title + " " + note, requester=meta.get("sender") or body.get("requester"), channel=meta.get("channel_name"), replies=meta.get("replies", 0))
    item = dict(title=title[:300], note=note, context=(body.get("context") or "").strip(), granola=(body.get("granola") or "").strip(), model=(body.get("model") or None),
                body=(meta.get("text") or "")[:3000], owner="me", requester=meta.get("sender") or body.get("requester"),
                channel=meta.get("channel_name"), source_type="slack" if link else "manual", permalink=link or None,
                route=sc["route"], vip_tier=sc["vip_tier"], scores=sc["scores"], autonomy_total=sc["total"], band=sc["band"], rationale=sc["rationale"],
                lift=body.get("lift") or {"PASS": "light", "WARN": "medium", "BLOCK": "heavy"}.get(sc["band"]))
    if link:
        try: item["slack_context"] = json.dumps(slack.context(link, hours=int(body.get("hours") or 24)), ensure_ascii=False)
        except Exception as e: item["slack_context"] = json.dumps({"error": str(e)[:200]})
    iid = db.insert_item(**item); triage.receipt(item["title"], sc["scores"], sc["band"], iid)
    ev = _intake_pg(item, meta)
    if ev: db.update_item(iid, intake_event_id=ev)
    if body.get("start"):
        try: ensure_live(db.get_item(iid), body.get("seed"))
        except Exception as e: db.update_item(iid, rationale=(item["rationale"] + f" | start failed: {e}")[:500])
    return items_with_live_one(iid)

def action(iid, kind, body):
    it = db.get_item(iid)
    if not it: return {"error": "no such item"}
    if kind == "start": return {"live": ensure_live(it, body.get("seed")), "item": db.get_item(iid)}
    if kind == "send":
        lv = ensure_live(it); text = body.get("text", "")
        paths = [p for p in (body.get("images") or []) if os.path.isfile(p)]
        if paths: text = (text + "\n" if text else "") + " ".join(paths)   # Claude Code attaches image files given by path
        live.send(lv["pane"], text); return {"ok": True}
    if kind == "upload":
        import base64, re as _re
        name = _re.sub(r"[^\w.-]", "_", body.get("name") or "image.png")[:80]
        d = os.path.join(os.path.expanduser("~/.command-center/uploads"), iid); os.makedirs(d, exist_ok=True)
        path = os.path.join(d, time.strftime("%Y%m%d-%H%M%S-") + name)
        open(path, "wb").write(base64.b64decode((body.get("data") or "").split(",")[-1]))
        return {"path": path}
    if kind == "prd_live":
        lv = ensure_live(it)
        live.send(lv["pane"], "/prd-authoring " + it["title"] + (f" (Shipyard {it['shipyard_key']})" if it.get("shipyard_key") else "")
                  + ". Publish to Shipyard and reply with the document URL on its own line.")
        return {"ok": True}
    if kind == "model":
        m = MODELS.get(body.get("model") or "", body.get("model")); db.update_item(iid, model=body.get("model"))
        lv = _live_index().get(it.get("live_sid") or "")
        if lv and m:
            live.send(lv["pane"], f"/model {m}"); time.sleep(1.5); live.press(lv["pane"], "Enter")   # confirms "Switch model?"
        return {"ok": True, "model": m, "sent": bool(lv)}
    if kind == "interrupt":
        lv = _live_index().get(it.get("live_sid") or "")
        if not lv: return {"error": "no live session"}
        live.press(lv["pane"], "Escape")
        return {"ok": True}
    if kind == "dbr_done":
        lv = _live_index().get(it.get("live_sid") or "")
        if lv: live.send(lv["pane"], "run dbr done")
        return {"ok": bool(lv)}
    if kind == "ask": return {"reply": cs.ask(it), "item": db.get_item(iid)}
    if kind == "prd": return {"reply": cs.prd(it), "item": db.get_item(iid)}
    if kind == "chat": return {"reply": cs.send(it, body.get("text", "")), "item": db.get_item(iid)}
    if kind == "team":
        assignee = body.get("assignee") or it.get("assignee")
        if assignee not in TEAM:
            return {"error": "Set the Assignee field first, then hand it off.", "need_assignee": True,
                    "options": [{"login": k, "name": v[0]} for k, v in TEAM.items()]}
        name, uid, slack_uid = TEAM[assignee]
        db.update_item(iid, owner="team", assignee=assignee)
        if it.get("shipyard_key"):
            # dbr done closes the ticket as Justin's; hand it over open, not done
            try: shipyard.call("update_task", task_id=it["shipyard_key"], assignee_id=uid, status="todo")
            except Exception as e: print(f"shipyard assign failed: {e}", file=sys.stderr, flush=True)
        links = []
        if it.get("prd_url"): links.append("PRD: " + it["prd_url"])
        if it.get("shipyard_key"): links.append(f"Ticket: https://{SHIPYARD_HOST}/t/" + it["shipyard_key"])
        text = HANDOFF_TEMPLATE.format(title=it["title"][:200], links="\n".join(links) or "No PRD yet, I will send one.")
        if body.get("preview"): return {"preview": text, "to": name}
        sent = None
        try: sent = slack.dm(slack_uid, text)
        except Exception as e: sent = {"error": str(e)[:200]}
        # close the loop: tell the session, run dbr done in it, then archive the card
        lv = _live_index().get(it.get("live_sid") or "")
        if lv:
            live.send(lv["pane"], f"Handed this to {name}. Run dbr done to close it out.")
            time.sleep(1.0); live.send(lv["pane"], "run dbr done")
            threading.Thread(target=_claim_after_dbr, args=(iid, uid), daemon=True).start()
        db.update_item(iid, status="archived", archived_at=db.now())
        return {"item": db.get_item(iid), "dm": sent, "text": text, "to": name, "archived": True, "dbr": bool(lv)}
    if kind == "done":
        db.update_item(iid, status="archived", archived_at=db.now())
        return {"item": db.get_item(iid)}
    if kind == "open_terminal":
        return {"cmd": sessions.open_in_terminal(it["claude_session_id"], None)} if it.get("claude_session_id") else {"error": "no session yet"}
    return {"error": "unknown action"}

def team_view(refresh=False):
    if refresh: github.refresh_team()
    out = []
    for login, (name, uid, _slack) in TEAM.items():
        c = github.team_card(login)
        try:
            mine = [shipyard.enrich(t) for t in shipyard.open_tasks(assignee_uid=uid, reporter_uid=ME_UID)]
            allt = [shipyard.enrich(t) for t in shipyard.open_tasks(assignee_uid=uid)]
            mk = {t["key"] for t in mine}; others = [t for t in allt if t["key"] not in mk]
        except Exception as e:
            mine, others = [], [{"key": "—", "title": f"shipyard unavailable: {e}"[:80], "status": "", "complexity": "", "progress": 0}]
        # Backlog is a wish list, not work in hand. Load counts only tickets that are actually moving.
        active = [t for t in mine + others if t.get("status") in ("todo", "in_progress", "in_review")]
        load = len(active) + c["week"] / 5
        handed = [{"key": (i.get("shipyard_key") or "—"), "title": i["title"][:90],
                   "url": (f"https://{SHIPYARD_HOST}/t/" + i["shipyard_key"]) if i.get("shipyard_key") else (i.get("prd_url") or "#"),
                   "status": "handed off", "complexity": {"light": "S", "medium": "M", "heavy": "L"}.get(i.get("lift") or "", ""),
                   "progress": 5, "prd_url": i.get("prd_url"), "at": i.get("updated_at")}
                  for i in db.list_items("all") if i.get("assignee") == login and i.get("owner") == "team"]
        c.update({"handed_off": handed, "tickets_from_me": mine, "tickets_from_others": others, "active_tickets": len(active),
                  "backlog_tickets": len(mine) + len(others) - len(active), "load": round(load, 1),
                  "bandwidth": "available" if load < 4 else "normal" if load < 9 else "loaded"})
        out.append(c)
    return out

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, payload, ctype="application/json"):
        data = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload, default=str).encode()
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(data))); self.send_header("Cache-Control", "no-store"); self.end_headers(); self.wfile.write(data)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0); return json.loads(self.rfile.read(n) or b"{}") if n else {}
    def do_GET(self):
        p = urllib.parse.urlparse(self.path); path, qs = p.path, urllib.parse.parse_qs(p.query); seg = path.strip("/").split("/")
        try:
            if path == "/": return self._send(200, open(os.path.join(HERE, "static", "index.html"), "rb").read(), "text/html; charset=utf-8")
            if path == "/api/config": return self._send(200, {"shipyard": SHIPYARD_HOST, "gh_org": GH_ORG, "team_name": TEAM_NAME, "roster": [{"login": k, "name": v[0]} for k, v in TEAM.items()]})
            if path == "/api/team/roster": return self._send(200, [{"login": k, "name": v[0]} for k, v in TEAM.items()])
            if path == "/api/items": return self._send(200, items_with_live("archived" if "archived" in qs else "open", (qs.get("sort") or ["lift"])[0]))
            if path == "/api/live": return self._send(200, live.list_live())
            if len(seg) == 4 and seg[:2] == ["api", "live"] and seg[3] == "transcript":
                sid = seg[2]; s_ = next((x for x in live.list_live() if x["sid"] == sid), {})
                msgs = live.transcript(sid, include_tools="tools" in qs)
                for it in db.list_items():
                    if it.get("live_sid") == sid and msgs: cs.pin_links(it, "\n".join(m["text"] for m in msgs if m["role"] == "assistant"))
                return self._send(200, {"messages": msgs, "screen": live.screen(s_.get("pane")) if "screen" in qs else None, "status": s_.get("status")})
            if len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "messages":
                it = db.get_item(seg[2]) or {}
                if it.get("live_sid"):
                    msgs = live.transcript(it["live_sid"], limit=40, include_tools="tools" in qs)
                    cs.pin_links(it, "\n".join(m["text"] for m in msgs if m["role"] == "assistant"))
                    lv = _live_index().get(it["live_sid"])
                    return self._send(200, {"messages": msgs, "status": lv["status"] if lv else "gone", "last_tool": live.last_tool(it["live_sid"]) if lv and lv["status"] == "busy" else None})
                return self._send(200, {"messages": [{"role": m["role"], "text": m["content"]} for m in db.messages(seg[2])], "status": None})
            if path == "/api/prs/mine":
                prs = github.my_open_prs() if "refresh" in qs else (db.prs_by_author(ME) or github.my_open_prs())
                if "enrich" in qs:
                    for pr in prs: pr.update(github.enrich_pr(pr["url"]))
                return self._send(200, prs)
            if path == "/api/team": return self._send(200, team_view(refresh="refresh" in qs))
            if path == "/api/roadmap": return self._send(200, roadmap.view(auto_map="map" in qs))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path; body = self._body(); seg = path.strip("/").split("/")
        try:
            if path == "/api/items": return self._send(201, create_item(body))
            if len(seg) == 4 and seg[:2] == ["api", "live"]:
                s_ = next((x for x in live.list_live() if x["sid"] == seg[2]), None)
                if not s_: return self._send(404, {"error": "session not live"})
                if seg[3] == "send": live.send(s_["pane"], body.get("text", "")); return self._send(200, {"ok": True})
                if seg[3] == "focus": live.focus(s_["pane"]); return self._send(200, {"ok": True})
            if len(seg) == 4 and seg[:2] == ["api", "items"]: return self._send(200, action(seg[2], seg[3], body))
            if path == "/api/roadmap/map": return self._send(200, {"mapped": roadmap.classify(roadmap.open_team_tasks())})
            if path == "/api/prs/monitor":
                url = body.get("url", ""); m = __import__("re").match(rf"https://github\.com/{GH_ORG}/([\w.-]+)/pull/(\d+)", url)
                if not m: return self._send(400, {"error": f"not a {GH_ORG} PR url"})
                repo, num = m.group(1), m.group(2); cwd = os.path.expanduser(f"~/work/{repo}")
                if not os.path.isdir(cwd): cwd = os.path.expanduser("~/work")
                iid = db.insert_item(title=f"Monitor {repo} #{num}", owner="me", source_type="pr", route="me", cwd=cwd, model=body.get("model"))
                db.update_item(iid, links=json.dumps([{"kind": "pr", "url": url}]))
                seed = body.get("seed") or (f"/github-pr-monitor {url}\nMonitor this PR: CI status, bot review threads, mergeability. "
                                            "Fix and reply to review threads per the skill, and tell me when it is ready to merge or needs me.")
                try: lv = ensure_live(db.get_item(iid), seed)
                except Exception as e: return self._send(500, {"error": str(e)[:300], "item": db.get_item(iid)})
                return self._send(201, {"item": items_with_live_one(iid), "live": lv})
            if path == "/api/prs/action":
                import subprocess
                url, act = body.get("url", ""), body.get("action", "")
                if not url.startswith(f"https://github.com/{GH_ORG}/"): return self._send(400, {"error": f"not a {GH_ORG} PR"})
                cmd = {"update": ["gh", "pr", "update-branch", url],
                       "automerge": ["gh", "pr", "merge", url, "--squash", "--auto"],
                       "ready": ["gh", "pr", "ready", url]}.get(act)
                if not cmd: return self._send(400, {"error": "unknown action"})
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
                return self._send(200 if r.returncode == 0 else 500, {"ok": r.returncode == 0, "out": (r.stdout + r.stderr).strip()[-400:]})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})
    def do_PATCH(self):
        path = urllib.parse.urlparse(self.path).path; body = self._body(); seg = path.strip("/").split("/")
        try:
            if len(seg) == 3 and seg[:2] == ["api", "items"]:
                allowed = {k: v for k, v in body.items() if k in ("title", "note", "owner", "assignee", "shipyard_key", "prd_url", "status", "route", "live_sid", "permalink", "requester", "channel", "context", "granola", "model", "lift")}
                if "links" in body: allowed["links"] = json.dumps(body["links"])
                if allowed.get("permalink"):
                    try:
                        meta = slack.fetch(allowed["permalink"]) or {}
                        allowed.setdefault("requester", meta.get("sender")); allowed.setdefault("channel", meta.get("channel_name"))
                        allowed["slack_context"] = json.dumps(slack.context(allowed["permalink"]), ensure_ascii=False)
                    except Exception: pass
                db.update_item(seg[2], **allowed)
                # anything added to the item after the session started goes straight into the chat
                infuse = {k: allowed[k] for k in ("note", "granola", "permalink", "shipyard_key", "prd_url") if k in allowed and allowed[k]}
                it2 = db.get_item(seg[2]); lv = _live_index().get(it2.get("live_sid") or "")
                if infuse and lv:
                    if "permalink" in infuse and it2.get("slack_context"): infuse["slack_context"] = _sc(it2)
                    if "note" in infuse: infuse["first_look_note"] = infuse.pop("note")
                    live.send(lv["pane"], "Updated context from my command center (fold this in, no need to reply at length):\n```json\n"
                              + json.dumps(infuse, indent=2, ensure_ascii=False) + "\n```")
                return self._send(200, it2)
            if len(seg) == 4 and seg[:2] == ["api", "roadmap"] and seg[2] == "map":
                db.set_rock(seg[3], body.get("rock_id", "none"), body.get("rationale", "")); return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})

if __name__ == "__main__":
    port = int(os.environ.get("CC_PORT", "8765"))
    db.conn().close(); db.migrate_v1()
    threading.Thread(target=_watcher, daemon=True).start()
    print(f"command-center on http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
