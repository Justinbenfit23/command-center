#!/usr/bin/env python3
"""Command Center v2. Intake list with a nested Claude session per item; team activity by day + Shipyard load;
big-rocks roadmap. Stdlib only. Run: python3 server.py -> http://127.0.0.1:8765 (or `cc`)."""
import json, os, sys, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, HERE)
from cc import db, slack, triage, github, sessions, shipyard, roadmap, live, usage, epd, claude_sessions as cs
from cc.config import ME, ME_UID, TEAM, TEAM_ID, TEAM_NAME, GH_ORG, SHIPYARD_HOST, ACCEL_DIR, HANDOFF_TEMPLATE, EPD_ARTIFACT_URL

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
    tds = db.todos_for([it["id"] for it in items])
    for it in items:
        it["todos"] = tds.get(it["id"], [])
        x = idx.get(it.get("live_sid")) if it.get("live_sid") else None
        it["live"] = {"status": x["status"], "pane": x["pane"], "kind": x["kind"]} if x else ({"status": "booting", "pane": it["live_pane"], "kind": "interactive"} if it.get("live_pane") and it.get("pending_seed") else None)
        if x and x.get("title") and it.get("source_type") == "live" and it["title"] in (x.get("name"), "Claude session"): 
            db.update_item(it["id"], title=x["title"]); it["title"] = x["title"]
    return items

def items_with_live_one(iid):
    it = db.get_item(iid); x = _live_index().get(it.get("live_sid") or "")
    it["live"] = {"status": x["status"], "pane": x["pane"], "kind": x["kind"]} if x else None
    it["todos"] = db.todos(iid)
    return it

MODELS = {"fable": "claude-fable-5-1", "opus": "claude-opus-5", "sonnet": "claude-sonnet-5"}
# How many interactive sessions may run at once. Measured 2026-09-24: ~1.9 GB each (Claude ~0.55 GB + the MCP
# servers every session spawns ~1.35 GB). 37 at once was ~70 GB on a 32 GB machine and took the tmux server
# down with the Terminal. Ten is ~19 GB, which leaves room for Chrome and Slack.
MAX_LIVE = int(os.environ.get("CC_MAX_LIVE", "24"))
MIN_FREE_GB = float(os.environ.get("CC_MIN_FREE_GB", "4"))

def live_count():
    return sum(1 for x in live.list_live() if x.get("kind") != "bg")

def mem_view():
    m = live.mem(); m["live"] = live_count(); m["cap"] = MAX_LIVE; m["tmux"] = live.server_up()
    m["room"] = max(0, MAX_LIVE - m["live"]); return m

def _cap_check(force=False):
    n = live_count(); m = live.mem()
    if force: return
    if n >= MAX_LIVE:
        raise RuntimeError(f"{n} Claude sessions are live and the cap is {MAX_LIVE}. Tick a finished one done (that ends it), "
                           f"or start with CC_MAX_LIVE={MAX_LIVE + 8} to raise it.")
    if m.get("free_gb") is not None and m["free_gb"] < MIN_FREE_GB:
        raise RuntimeError(f"Only {m['free_gb']} GB free and a session needs ~1.5 GB. Tick a finished session done to end it, "
                           f"then try again ({n} live now).")

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
           "Then go do the work the note asks for. Do not wait for me to approve it: the note is the go-ahead. "
           "Stop and ask only if the work is destructive, outward-facing, or the note is genuinely ambiguous about what I want." if not item.get("shipyard_key") else
           f"This is Shipyard {item['shipyard_key']}. Read it, then give me a three-sentence read on where it stands and the next step, "
           "then go do that next step. Do not wait for me to approve it: the ticket is the go-ahead. "
           "Stop and ask only if the work is destructive, outward-facing, or genuinely ambiguous.")
    return (TODO_HOWTO + "Intake item from my command center. Everything I have is in this JSON; treat `first_look_note` as my instructions and context, "
            "`granola` as meeting notes: if it is a notes.granola.ai link, OPEN IT FIRST with the Chrome browser tools (tabs_context_mcp, navigate, then get_page_text; the page renders client-side and needs my logged-in Chrome) and treat the page text as the notes; if it is pasted text, it already is the notes. "
            "`slack_context` is the conversation that led up to the linked message "
            "(`before` = the prior 24h in that channel/DM, `thread` = the linked thread). Read it before you answer.\n\n```json\n" + json.dumps(ctx, indent=2, ensure_ascii=False) + "\n```\n\n" + ask)

TODO_HOWTO = ("This item has a to-do list in my command center. It is MINE: do NOT push checklists, plans or steps to it on your own. "
              "Add a line ONLY when I explicitly ask (\"add that to my to-do\", \"put it on the list\"), and then only the lines I asked for, with:\n"
              "curl -s -X POST http://127.0.0.1:8765/api/todos/push -H 'Content-Type: application/json' "
              "-d '{\"key\": \"<GTMP key>\", \"items\": [\"the one line I asked for\"]}'\n"
              "(use \"sid\": \"<your session id>\" before a ticket exists). GET /api/todos/push?key=<key> reads it; PATCH /api/todos/<id> {\"done\": true} "
              "marks a line done when I ask you to.\n\n")

def ensure_live(item, seed=None, force=False):
    """Start a real tmux Claude session for this item if it has none. Returns the live dict.
    If claude is still booting when we give up waiting, the pane and seed are parked on the item and
    the watcher thread sends the seed the moment the session registers."""
    if item.get("live_sid") and item["live_sid"] in _live_index(): return _live_index()[item["live_sid"]]
    if item.get("live_pane") and item.get("pending_seed"): return {"sid": None, "pane": item["live_pane"], "status": "booting"}
    _cap_check(force)
    old = item.get("live_sid")
    if old:
        # The session exists but its process is gone (tmux died, Terminal killed, reboot): resume it, never
        # start a fresh one, so the conversation carries on where it stopped. It must run in its own cwd.
        cwd = live.transcript_cwd(old) or item.get("cwd") or None
        sid, pane = live.spawn(cwd=cwd, resume=old)
        live.rename_pane(pane, item["title"])
        if sid and sid != old: db.update_item(item["id"], live_sid=sid)
        text = seed if seed is not None else ""
        if not sid:
            db.update_item(item["id"], live_pane=pane, pending_seed=text or None); return {"sid": None, "pane": pane, "status": "booting"}
        db.update_item(item["id"], live_pane=pane)
        if text: time.sleep(2.5); live.send(pane, text)
        return {"sid": sid, "pane": pane, "status": "busy", "resumed": True}
    sid, pane = live.spawn(cwd=item.get("cwd") or None, model=MODELS.get(item.get("model") or "", item.get("model")))
    live.rename_pane(pane, item["title"])
    text = seed if seed is not None else seed_for_live(item)
    if not sid:
        db.update_item(item["id"], live_pane=pane, pending_seed=text); return {"sid": None, "pane": pane, "status": "booting"}
    db.update_item(item["id"], live_sid=sid, live_pane=pane)
    time.sleep(2.5); live.send(pane, text)
    return {"sid": sid, "pane": pane, "status": "busy"}

def resume_sessions(limit=None, sids=None, force=False):
    """Bring dead sessions back: with no sids, every stopped OPEN item; with sids, exactly those (archived ones reopen). Stops at the cap."""
    idx = _live_index(); done, skipped = [], []
    items = db.list_items("open") + db.list_items("archived")
    by_sid = {}
    for it in items:   # open items are listed first, so an open item wins over an archived twin sharing its session
        if it.get("live_sid") and it["live_sid"] not in by_sid: by_sid[it["live_sid"]] = it
    order = [r["sid"] for r in db.roster()]
    for it in items:
        if it.get("live_sid") and it["live_sid"] not in order: order.append(it["live_sid"])
    targets = [x for x in (sids or order) if x not in idx and x != "" and live._transcript_path(x)]
    if not sids:
        # bulk resume = OPEN items only. An archived item was ticked done on purpose; it comes back only by its own Resume button.
        targets = [x for x in targets if x in by_sid and by_sid[x].get("status") not in db.ARCHIVED]
    for sid in targets:
        if limit is not None and len(done) >= limit: break
        it = by_sid.get(sid)
        if not it:
            r = next((r for r in db.roster() if r["sid"] == sid), {})
            iid = db.insert_item(title=r.get("title") or "Claude session", owner="me", source_type="live", live_sid=sid, route="me", cwd=r.get("cwd"))
            it = db.get_item(iid)
        try:
            lv = ensure_live(it, seed="", force=force); done.append({"sid": sid, "item": it["id"], "title": it["title"], "pane": lv.get("pane")})
        except RuntimeError as e:
            skipped.append({"sid": sid, "title": it["title"], "why": str(e)}); break
        except Exception as e:
            skipped.append({"sid": sid, "title": it["title"], "why": str(e)[:200]})
        if it.get("status") in db.ARCHIVED: db.update_item(it["id"], status="open", archived_at=None)
    return {"resumed": done, "skipped": skipped, "mem": mem_view()}

def _end_session(iid, why):
    """Kill the tmux pane behind an item. A handed-off or finished card must not leave a Claude
    process running — each one holds ~2GB, and an idle session is the whole cost with none of the use.
    The transcript stays on disk, so Resume on the archived row still brings it back."""
    it = db.get_item(iid)
    if not it: return False
    lv = _live_index().get(it.get("live_sid") or "")
    pane = (lv or {}).get("pane") or it.get("live_pane"); pid = (lv or {}).get("pid")
    if not (pane or pid): return False
    try: live.kill(pane, pid)
    except Exception as e: print(f"{why}: kill failed: {e}", file=sys.stderr, flush=True); return False
    db.update_item(iid, live_pane=None, pending_seed=None)
    print(f"{why}: ended session for item {iid}", file=sys.stderr, flush=True)
    return True

def pick_assignee(it):
    """Read the card's chat and say who it is being handed to. Justin has usually already said so in the
    conversation, so the team button should not make him pick again. One `claude -p` call on a small
    model, no MCP servers, neutral cwd (so ~/work hooks and CLAUDE.md do not load). Returns a TEAM login or None."""
    import subprocess
    cands = [k for k in TEAM if k != ME]
    msgs = live.transcript(it["live_sid"], limit=60) if it.get("live_sid") else [{"role": m["role"], "text": m["content"]} for m in db.messages(it["id"])]
    chat = "\n\n".join(f"{'JUSTIN' if m['role'] == 'user' else 'CLAUDE'}: {m['text'][:1500]}" for m in msgs)[-14000:]
    roster = "\n".join(f"- {k}: {TEAM[k][0]}" for k in cands)
    prompt = (f"Justin is handing this work item to one teammate. Decide who from the chat below: he usually names the "
              f"person, or describes work that clearly belongs to one of them.\n\nTeammates (login: name):\n{roster}\n\n"
              f"Item: {it['title']}\nNote: {(it.get('note') or '')[:800]}\n\nChat:\n{chat}\n\n"
              f"Reply with exactly one login from the list and nothing else. If the chat gives no basis to choose, reply NONE.")
    cmd = ["claude", "-p", prompt, "--model", "claude-haiku-4-5-20251001", "--output-format", "text", "--allowedTools", "",
           "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=os.path.expanduser("~/.command-center"))
        out = (r.stdout or "").strip().lower()
    except Exception as e:
        print(f"pick_assignee failed: {e}", file=sys.stderr, flush=True); return None
    hits = [k for k in cands if k.lower() in out] or [k for k in cands if TEAM[k][0].split()[0].lower() in out]
    print(f"pick_assignee: {out[:80]!r} -> {hits}", file=sys.stderr, flush=True)
    return hits[0] if len(hits) == 1 else None

def _handoff_notify(iid, assignee):
    """Move the ticket to the assignee and Slack them the handoff. Runs after dbr done so the ticket key
    the session produced is on the card. A failed DM becomes a to-do on the card instead of vanishing."""
    it = db.get_item(iid)
    if not it: return None
    name, uid, slack_uid = TEAM[assignee]
    if it.get("shipyard_key"):
        try: shipyard.call("update_task", task_id=it["shipyard_key"], assignee_id=uid, status="todo")
        except Exception as e: print(f"shipyard assign failed: {e}", file=sys.stderr, flush=True)
    links = []
    if it.get("prd_url"): links.append("PRD: " + it["prd_url"])
    if it.get("shipyard_key"): links.append(f"Ticket: https://{SHIPYARD_HOST}/t/" + it["shipyard_key"])
    text = HANDOFF_TEMPLATE.format(title=it["title"][:200], links="\n".join(links) or "No PRD yet, I will send one.")
    try: sent = slack.dm(slack_uid, text); err = (sent or {}).get("error") if isinstance(sent, dict) else None
    except Exception as e: err = str(e)[:200]
    if err:
        db.add_todos(iid, [f"Slack DM to {name} failed ({err}). Send the handoff by hand."], source="cc")
        print(f"handoff DM to {name} failed: {err}", file=sys.stderr, flush=True)
    else: print(f"handoff DM sent to {name} for item {iid}", file=sys.stderr, flush=True)
    return err

def _claim_after_dbr(iid, assignee, wait=420):
    """dbr done often creates or closes the ticket. Once the session goes idle, pin whatever key it
    produced onto the item, hand the ticket over open rather than done, and END the session — the card
    is already archived, so leaving Claude running just holds memory."""
    end = time.time() + wait
    while time.time() < end:
        time.sleep(10)
        it = db.get_item(iid)
        if not it: return
        lv = _live_index().get(it.get("live_sid") or "")
        if lv and lv["status"] == "busy": continue
        msgs = live.transcript(it["live_sid"], limit=30) if it.get("live_sid") else []
        text = "\n".join(m["text"] for m in msgs if m["role"] == "assistant")
        if msgs: cs.pin_links(it, text)
        # dbr done closes the ticket for the work just finished and spins off a follow-up for what is
        # left. The spin-off is created last, so the highest key in the transcript is the one to hand over.
        keys = sorted(set(cs.KEY.findall(text)), key=lambda k: int(k.split("-")[1]))
        key = keys[-1] if keys else (db.get_item(iid) or {}).get("shipyard_key")
        if key: db.update_item(iid, shipyard_key=key)
        _handoff_notify(iid, assignee)
        _end_session(iid, "handoff")
        return
    # dbr done never went idle inside the window. The card is handed off and archived either way, so
    # notify anyway and reclaim the memory rather than leaving an orphan running all day.
    _handoff_notify(iid, assignee)
    _end_session(iid, "handoff timeout")

def _watcher():
    """Every 2s: finish any session that registered after we stopped waiting, and answer trust prompts on the way."""
    while True:
        try:
            for it in db.list_items():
                # a pane still booting: either a fresh session (no sid yet) or a resume waiting to register
                if it.get("live_pane") and (not it.get("live_sid") or (it.get("pending_seed") and it["live_sid"] not in _live_index())):
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
    if kind == "start":
        lv = ensure_live(it, body.get("seed"))
        if it.get("status") in db.ARCHIVED: db.update_item(iid, status="open", archived_at=None)   # resuming an archived item reopens it
        return {"live": lv, "item": db.get_item(iid)}
    if kind == "send":
        lv = ensure_live(it); text = body.get("text", "")
        if lv.get("sid") and live.pending_question(lv["sid"]):
            return {"error": "Claude is asking you a question in the terminal. Answer it there first (Open terminal), then send this."}
        paths = [p for p in (body.get("images") or []) if os.path.isfile(p)]
        if paths:   # dropped files reach the session as paths on their own lines, which it Reads
            label = "Attached file:" if len(paths) == 1 else f"Attached {len(paths)} files:"
            text = (text + "\n\n" if text else "") + label + "\n" + "\n".join(paths)
        live.send(lv["pane"], text); return {"ok": True}
    if kind == "upload":
        import base64, re as _re
        name = _re.sub(r"[^\w.-]", "_", body.get("name") or "file.bin")[:80]
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
        # One click, then walk away: pick the assignee from the chat, run dbr done in the session, Slack the
        # handoff (after dbr done, so the ticket key is on the card), end the session, archive the card.
        assignee = body.get("assignee") or it.get("assignee")
        if assignee not in TEAM or assignee == ME:
            assignee = pick_assignee(it)
        if assignee not in TEAM:
            return {"error": "Couldn't tell from the chat who this goes to. Set the Assignee field on the card, then press team again.",
                    "need_assignee": True, "options": [{"login": k, "name": v[0]} for k, v in TEAM.items() if k != ME]}
        name = TEAM[assignee][0]
        # A preview must not move the card. It only answers "who did you read out of the chat?" so the
        # UI can confirm before a DM leaves the building.
        if body.get("preview"):
            return {"to": name, "assignee": assignee, "guessed": not (body.get("assignee") or it.get("assignee"))}
        db.update_item(iid, owner="team", assignee=assignee)
        lv = _live_index().get(it.get("live_sid") or "")
        if lv:
            live.send(lv["pane"], f"Handed this to {name}. Run dbr done to close it out.")
            time.sleep(1.0); live.send(lv["pane"], "run dbr done")
            threading.Thread(target=_claim_after_dbr, args=(iid, assignee), daemon=True).start()
        else:
            threading.Thread(target=_handoff_notify, args=(iid, assignee), daemon=True).start()
        db.update_item(iid, status="archived", archived_at=db.now())
        return {"item": db.get_item(iid), "to": name, "assignee": assignee, "archived": True, "dbr": bool(lv)}
    if kind == "done":
        # Done means done: the session is ended, not left running behind the archive. Its transcript
        # stays on disk, so Resume on the archived row brings it back if it is ever needed.
        lv = _live_index().get(it.get("live_sid") or "")
        pane = (lv or {}).get("pane") or it.get("live_pane"); pid = (lv or {}).get("pid")
        if pane or pid: live.kill(pane, pid)
        db.update_item(iid, status="archived", archived_at=db.now(), live_pane=None, pending_seed=None)
        return {"item": db.get_item(iid), "killed": bool(pane or pid)}
    if kind == "stop":
        lv = _live_index().get(it.get("live_sid") or "")
        pane = (lv or {}).get("pane") or it.get("live_pane"); pid = (lv or {}).get("pid")
        if pane or pid: live.kill(pane, pid)
        db.update_item(iid, live_pane=None, pending_seed=None)
        return {"item": db.get_item(iid), "killed": bool(pane or pid)}
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
            if path == "/api/mem": return self._send(200, mem_view())
            if path == "/api/sessions":
                idx = _live_index(); by_sid = {it["live_sid"]: it for it in db.list_items("all") if it.get("live_sid")}
                out = []
                for r in db.roster():
                    it = by_sid.get(r["sid"], {})
                    out.append({**r, "live": r["sid"] in idx, "resumable": bool(live._transcript_path(r["sid"])), "item": it.get("id"),
                                "item_title": it.get("title"), "item_status": it.get("status")})
                return self._send(200, {"sessions": out, "mem": mem_view()})
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
                    q = live.pending_question(it["live_sid"]) if lv else None
                    return self._send(200, {"messages": msgs, "status": lv["status"] if lv else "gone", "last_tool": live.last_tool(it["live_sid"]) if lv and lv["status"] == "busy" else None,
                                            "question": q, "dialog": None if (q or not lv) else live.dialog(lv.get("pane"))})
                return self._send(200, {"messages": [{"role": m["role"], "text": m["content"]} for m in db.messages(seg[2])], "status": None})
            if len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "todos": return self._send(200, db.todos(seg[2]))
            if path == "/api/todos/push":
                it = db.item_by_key_or_sid(key=(qs.get("key") or [None])[0], sid=(qs.get("sid") or [None])[0])
                return self._send(200, {"item": it["id"], "todos": db.todos(it["id"])}) if it else self._send(404, {"error": "no item with that key or session"})
            if path == "/api/prs/mine":
                prs = github.my_open_prs() if "refresh" in qs else (db.prs_by_author(ME) or github.my_open_prs())
                if "enrich" in qs:
                    for pr in prs: pr.update(github.enrich_pr(pr["url"]))
                return self._send(200, prs)
            if path == "/api/team": return self._send(200, team_view(refresh="refresh" in qs))
            if path == "/api/usage":
                try: return self._send(200, usage.fetch(refresh="refresh" in qs))
                except Exception as e: return self._send(200, {"error": str(e)[:300]})
            if path == "/api/roadmap": return self._send(200, roadmap.view(auto_map="map" in qs))
            if path == "/api/epd":
                if "refresh" in qs:
                    try: epd.refresh_from_shipyard()
                    except Exception as e: print(f"epd refresh failed: {e}", file=sys.stderr, flush=True)
                return self._send(200, {"projects": epd.rows(team_view_only="team" in qs),
                                        "top_three": epd.top_three(), "phases": epd.PHASES,
                                        "delivery": epd.DELIVERY, "artifact": EPD_ARTIFACT_URL})
            # what the 15-minute sync reads to learn what this side believes
            if path == "/api/epd/export": return self._send(200, epd.export())
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})
    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path; body = self._body(); seg = path.strip("/").split("/")
        try:
            # the sync hands back the artifact's rows; we reconcile and tell it what to write up there
            if path == "/api/epd/merge": return self._send(200, epd.merge_remote(body.get("projects") or {}))
            if path == "/api/epd/synced":
                for k, at in (body.get("synced") or {}).items(): db.roadmap_mark_synced(k, at)
                return self._send(200, {"ok": True, "marked": len(body.get("synced") or {})})
            if path == "/api/items": return self._send(201, create_item(body))
            if path == "/api/sessions/resume":
                sids = [body["sid"]] if body.get("sid") else (body.get("sids") or None)
                return self._send(200, resume_sessions(limit=body.get("limit"), sids=sids, force=bool(body.get("force"))))
            if path == "/api/restart":
                # Under launchd (KeepAlive) a clean exit is a restart. Started by hand, re-exec in place instead.
                def _go():
                    time.sleep(0.4)
                    if os.environ.get("CC_LAUNCHD"): os._exit(0)
                    os.execv(sys.executable, [sys.executable] + sys.argv)
                threading.Thread(target=_go, daemon=True).start()
                return self._send(200, {"ok": True, "launchd": bool(os.environ.get("CC_LAUNCHD"))})
            if path == "/api/tmux/host":
                return self._send(200, {"host": live.ensure_host(), "tmux": live.server_up()})
            if len(seg) == 4 and seg[:2] == ["api", "live"]:
                s_ = next((x for x in live.list_live() if x["sid"] == seg[2]), None)
                if not s_: return self._send(404, {"error": "session not live"})
                if seg[3] == "send": live.send(s_["pane"], body.get("text", "")); return self._send(200, {"ok": True})
                if seg[3] == "focus": live.focus(s_["pane"]); return self._send(200, {"ok": True})
            if len(seg) == 4 and seg[:2] == ["api", "items"] and seg[3] == "todos":
                ids = db.add_todos(seg[2], body.get("items") or body.get("text") or [], source=body.get("source") or "me")
                return self._send(201, {"added": ids, "todos": db.todos(seg[2])})
            if path == "/api/todos/push":
                it = db.item_by_key_or_sid(key=body.get("key"), sid=body.get("sid"))
                if not it: return self._send(404, {"error": "no item with that key or session"})
                ids = db.add_todos(it["id"], body.get("items") or body.get("text") or [], source=body.get("source") or "claude")
                return self._send(201, {"item": it["id"], "added": ids, "todos": db.todos(it["id"])})
            if len(seg) == 4 and seg[:2] == ["api", "items"]: return self._send(200, action(seg[2], seg[3], body))
            if path == "/api/roadmap/map": return self._send(200, {"mapped": roadmap.classify(roadmap.open_team_tasks())})
            if path == "/api/prs/monitor":
                url = body.get("url", ""); m = __import__("re").match(rf"https://github\.com/{GH_ORG}/([\w.-]+)/pull/(\d+)", url)
                if not m: return self._send(400, {"error": f"not a {GH_ORG} PR url"})
                repo, num = m.group(1), m.group(2)
                # Always start in ~/work: the MCP servers and workspace CLAUDE.md are registered
                # at that project scope, and a session started inside the repo never loads them.
                cwd = os.path.expanduser("~/work")
                checkout = os.path.expanduser(f"~/work/{repo}")
                iid = db.insert_item(title=f"Monitor {repo} #{num}", owner="me", source_type="pr", route="me", cwd=cwd, model=body.get("model"))
                db.update_item(iid, links=json.dumps([{"kind": "pr", "url": url}]))
                seed = body.get("seed") or ((f"cd {checkout} first — the checkout is there.\n" if os.path.isdir(checkout) else "")
                                            + f"/github-pr-monitor {url}\nMonitor this PR: read the PR's Slack thread first (humans post blocking asks there that GitHub never shows), then CI status, bot review threads, mergeability. "
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
            if len(seg) == 3 and seg[:2] == ["api", "epd"]:
                return self._send(200, epd.db.roadmap_upsert(seg[2], by="justin", **body) or {})
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
            if len(seg) == 3 and seg[:2] == ["api", "todos"]:
                f = {k: body[k] for k in ("text", "done", "pos") if k in body}
                r = db.update_todo(seg[2], **f) if f else None
                return self._send(200, r) if r else self._send(404, {"error": "no such to-do"})
            if len(seg) == 4 and seg[:2] == ["api", "roadmap"] and seg[2] == "map":
                db.set_rock(seg[3], body.get("rock_id", "none"), body.get("rationale", "")); return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})

    def do_DELETE(self):
        path = urllib.parse.urlparse(self.path).path; seg = path.strip("/").split("/")
        try:
            if len(seg) == 3 and seg[:2] == ["api", "todos"]: db.delete_todo(seg[2]); return self._send(200, {"ok": True})
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
