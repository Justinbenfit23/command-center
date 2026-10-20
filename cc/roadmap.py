"""Roadmap tab, PRD grain: every PRD in flight, filed under the team's three mandates (ROCKS).

"In flight" = a project on the EPD board that is not Done (the team's PRD-level list), plus any PRD on one
of Justin's intake cards whose card is still open. One row per Shipyard document id, so the same PRD reached
both ways is one row. Justin's fields (his title, his notes, the rock, hidden) live in prd_rows and never get
overwritten by Update; Update only files PRDs that have no rock yet, with one `claude -p` call."""
import json, re, subprocess, threading
from .config import ROCKS, CLAUDE_CWD
from . import db, epd

ROCK_IDS = {r["id"] for r in ROCKS}
_DOC = re.compile(r"/d/([0-9a-f-]{36})")
_STATE = {"running": False, "error": "", "step": ""}

def doc_id(url):
    m = _DOC.search(url or ""); return m.group(1) if m else None

def in_flight():
    """{doc_id: {url, title, who, phase}} from the board (not Done) and open intake cards."""
    out = {}
    try: projects = epd.rows()
    except Exception as e: projects = []; _STATE["error"] = f"board read failed: {str(e)[:100]}"
    for p in projects:
        d = doc_id(p.get("link"))
        if d and d not in out:
            out[d] = {"url": p["link"], "title": p.get("title") or "", "who": (p.get("assignee") or "").capitalize(), "phase": p.get("phase") or ""}
    for it in db.list_items(status="open"):
        d = doc_id(it.get("prd_url"))
        if d and d not in out:
            out[d] = {"url": it["prd_url"], "title": it["title"], "who": "Me", "phase": "", "item": it["id"]}
    return out

def classify(rows):
    """File every PRD that has no rock yet. One claude -p call; returns how many were filed."""
    todo = {d: r for d, r in rows.items() if not r.get("rock_id")}
    if not todo: return 0
    rocks = "\n".join(f"- {r['id']}: {r['title']}. {r['blurb']}" for r in ROCKS)
    lines = "\n".join(json.dumps({"id": d, "title": r["title"]}) for d, r in todo.items())
    prompt = (f"File each PRD under one of a sales-ops team's three mandates.\nMandates:\n{rocks}\n\nPRDs:\n{lines}\n\n"
              "Use \"none\" only when a PRD plainly moves none of them. Reply ONLY with a JSON object {\"<id>\": \"<mandate id or none>\"}.")
    _STATE["step"] = f"filing {len(todo)} PRDs"
    try:
        r = subprocess.run(["claude", "-p", prompt, "--output-format", "json", "--model", "sonnet"],
                           capture_output=True, text=True, timeout=300, cwd=CLAUDE_CWD)
        txt = json.loads(r.stdout).get("result", ""); s, e = txt.find("{"), txt.rfind("}"); m = json.loads(txt[s:e + 1])
    except Exception as ex:
        _STATE["error"] = f"filing failed: {str(ex)[:120]}"; return 0
    n = 0
    for d, rid in m.items():
        if d in todo:
            db.prd_set(d, rock_id=rid if rid in ROCK_IDS else "none"); n += 1
    return n

def refresh():
    """The Update button: pull the board, then file any PRD that has no rock yet."""
    try:
        _STATE.update(running=True, error="", step="reading the EPD board")
        try: epd.pull()
        except Exception as e: _STATE["error"] = f"board pull failed: {str(e)[:100]}"
        mine = db.prd_rows()
        classify({d: {**f, **mine.get(d, {})} for d, f in in_flight().items()})
        db.set_setting("roadmap_updated_at", db.now())
    except Exception as e:
        _STATE["error"] = str(e)[:200]
    finally:
        _STATE.update(running=False, step="")

def start_refresh():
    if not _STATE["running"]:
        _STATE["running"] = True; threading.Thread(target=refresh, daemon=True).start()
    return {"running": True}

def view():
    mine = db.prd_rows(); flight = in_flight()
    rocks = [{**r, "prds": []} for r in ROCKS] + [{"id": "none", "title": "Not on a big rock", "blurb": "", "prds": []}]
    by = {r["id"]: r for r in rocks}; unfiled = 0
    for d, f in flight.items():
        m = mine.get(d, {})
        if m.get("hidden"): continue
        if not m.get("rock_id"): unfiled += 1
        by.get(m.get("rock_id"), by["none"])["prds"].append({
            "doc_id": d, "url": f["url"], "title": m.get("title") or f["title"], "source_title": f["title"],
            "notes": m.get("notes") or "", "notes_at": m.get("notes_at"), "who": f["who"], "phase": f["phase"], "item": f.get("item")})
    for r in rocks: r["prds"].sort(key=lambda p: p["title"].lower())
    return {"rocks": rocks, "updated_at": db.get_setting("roadmap_updated_at"), "unfiled": unfiled,
            **{k: _STATE[k] for k in ("running", "error", "step")}}
