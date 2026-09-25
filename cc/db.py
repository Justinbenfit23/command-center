"""SQLite store. `items` is the intake list; `item_messages` is each item's nested Claude thread."""
import os, sqlite3, uuid, json
from datetime import datetime
from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  note TEXT DEFAULT '',                 -- the operator's first-look note
  body TEXT DEFAULT '',                 -- fetched Slack text
  owner TEXT NOT NULL DEFAULT 'me',     -- me | team
  status TEXT NOT NULL DEFAULT 'open',  -- open | archived
  assignee TEXT, requester TEXT, vip_tier TEXT, channel TEXT, source_type TEXT, permalink TEXT,
  shipyard_key TEXT, prd_url TEXT,
  route TEXT DEFAULT 'me', scores TEXT DEFAULT '{}', autonomy_total INTEGER, band TEXT, rationale TEXT DEFAULT '',
  claude_session_id TEXT, intake_event_id INTEGER, links TEXT DEFAULT '[]', live_sid TEXT, context TEXT DEFAULT '', granola TEXT DEFAULT '', model TEXT, slack_context TEXT, cwd TEXT, live_pane TEXT, pending_seed TEXT, lift TEXT,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, archived_at TEXT
);
CREATE TABLE IF NOT EXISTS item_messages (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prs (
  url TEXT PRIMARY KEY, platform TEXT DEFAULT 'github', repo TEXT, number INTEGER, title TEXT, author TEXT,
  draft INTEGER DEFAULT 0, state TEXT, review TEXT, ci TEXT, updated_at TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS team_daily (login TEXT, day TEXT, commits INTEGER, PRIMARY KEY(login, day));
CREATE TABLE IF NOT EXISTS team_meta (login TEXT PRIMARY KEY, repos TEXT, fetched_at TEXT);
CREATE TABLE IF NOT EXISTS rock_map (task_key TEXT PRIMARY KEY, rock_id TEXT, rationale TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS item_todos (
  id TEXT PRIMARY KEY, item_id TEXT NOT NULL, text TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0,
  pos INTEGER NOT NULL DEFAULT 0, source TEXT DEFAULT 'me', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, done_at TEXT
);
CREATE INDEX IF NOT EXISTS item_todos_item ON item_todos(item_id, done, pos);
CREATE TABLE IF NOT EXISTS session_roster (
  sid TEXT PRIMARY KEY, cwd TEXT, title TEXT, pane TEXT, pid INTEGER, kind TEXT,
  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL
);
-- The EPD Rolling Roadmap rows. Shipyard owns title/assignee/status/link; these are the PM fields
-- Shipyard's API has no home for. Mirrored both ways with the claude.ai artifact by cc/epd.py.
CREATE TABLE IF NOT EXISTS roadmap (
  task_key TEXT PRIMARY KEY,
  assignee TEXT, phase TEXT, delivery TEXT, progress INTEGER, rank INTEGER,
  rock_id TEXT, rock_note TEXT, team_view INTEGER NOT NULL DEFAULT 1,
  summary TEXT DEFAULT '', week_of TEXT, start_date TEXT, target_date TEXT,
  title TEXT, link TEXT, link_kind TEXT, tickets TEXT DEFAULT '[]',
  updated_at TEXT NOT NULL, updated_by TEXT NOT NULL DEFAULT 'justin',
  remote_at TEXT,              -- updated_at of the artifact row we last reconciled against
  deleted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS roadmap_assignee ON roadmap(assignee, team_view, deleted);
"""

def conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=10); c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL"); c.executescript(SCHEMA)
    cols = [r[1] for r in c.execute("PRAGMA table_info(items)")]
    if "links" not in cols: c.execute("ALTER TABLE items ADD COLUMN links TEXT DEFAULT '[]'")
    if "live_sid" not in cols: c.execute("ALTER TABLE items ADD COLUMN live_sid TEXT")
    for col, ddl in (("context", "TEXT DEFAULT ''"), ("granola", "TEXT DEFAULT ''"), ("model", "TEXT"), ("slack_context", "TEXT"), ("cwd", "TEXT"), ("live_pane", "TEXT"), ("pending_seed", "TEXT"), ("lift", "TEXT")):
        if col not in cols: c.execute(f"ALTER TABLE items ADD COLUMN {col} {ddl}")
    return c

def now(): return datetime.now().isoformat(timespec="seconds")
def new_id(): return uuid.uuid4().hex[:12]
def rows(cur): return [dict(r) for r in cur.fetchall()]

def _load(t):
    if t:
        t["scores"] = json.loads(t.get("scores") or "{}")
        try: t["links"] = json.loads(t.get("links") or "[]")
        except Exception: t["links"] = []
    return t

def insert_item(**f):
    f.setdefault("id", new_id()); f["created_at"] = f["updated_at"] = now(); f["scores"] = json.dumps(f.get("scores") or {})
    with conn() as c: c.execute(f"INSERT INTO items ({','.join(f)}) VALUES ({','.join('?'*len(f))})", list(f.values()))
    return f["id"]

def update_item(iid, **f):
    if "scores" in f: f["scores"] = json.dumps(f["scores"])
    f["updated_at"] = now()
    with conn() as c: c.execute(f"UPDATE items SET {','.join(k+'=?' for k in f)} WHERE id=?", [*f.values(), iid])

def get_item(iid):
    with conn() as c: r = c.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone()
    return _load(dict(r)) if r else None

LIFT_ORDER = {"light": 0, "medium": 1, "heavy": 2}

ARCHIVED = ("archived", "done", "dropped")

def list_items(status="open", sort="lift"):
    """status: 'open' | 'archived' (covers done/dropped) | 'all'. sort: lift | added_desc | added_asc"""
    with conn() as c:
        if status == "all": out = rows(c.execute("SELECT * FROM items"))
        elif status == "open": out = rows(c.execute("SELECT * FROM items WHERE status NOT IN (?,?,?)", ARCHIVED))
        else: out = rows(c.execute("SELECT * FROM items WHERE status IN (?,?,?)", ARCHIVED))
    if sort == "added_asc": out.sort(key=lambda t: _ts(t.get("created_at")))
    elif sort == "added_desc": out.sort(key=lambda t: -_ts(t.get("created_at")))
    else: out.sort(key=lambda t: (LIFT_ORDER.get(t.get("lift") or "", 1), -_ts(t.get("created_at"))))
    return [_load(t) for t in out]

def _ts(iso):
    try: return datetime.fromisoformat(iso).timestamp()
    except Exception: return 0

def add_message(iid, role, content):
    with conn() as c: c.execute("INSERT INTO item_messages VALUES(?,?,?,?,?)", (new_id(), iid, role, content, now()))

def messages(iid):
    with conn() as c: return rows(c.execute("SELECT role,content,created_at FROM item_messages WHERE item_id=? ORDER BY created_at", (iid,)))

def upsert_prs(items):
    with conn() as c:
        for p in items:
            c.execute("""INSERT INTO prs(url,platform,repo,number,title,author,draft,state,review,ci,updated_at,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(url) DO UPDATE SET title=excluded.title,draft=excluded.draft,state=excluded.state,updated_at=excluded.updated_at,fetched_at=excluded.fetched_at""",
                      (p["url"], "github", p["repo"], p["number"], p["title"], p["author"], int(p.get("draft", 0)), p.get("state", "open"), p.get("review"), p.get("ci"), p.get("updated_at"), now()))

def prs_by_author(author):
    with conn() as c: return rows(c.execute("SELECT * FROM prs WHERE author=? AND state='open' ORDER BY updated_at DESC", (author,)))

def set_daily(login, day_counts, repos):
    with conn() as c:
        c.execute("DELETE FROM team_daily WHERE login=?", (login,))
        c.executemany("INSERT INTO team_daily VALUES(?,?,?)", [(login, d, n) for d, n in day_counts.items()])
        c.execute("INSERT OR REPLACE INTO team_meta VALUES(?,?,?)", (login, json.dumps(repos), now()))

def get_daily(login):
    with conn() as c:
        days = {r["day"]: r["commits"] for r in c.execute("SELECT day,commits FROM team_daily WHERE login=?", (login,))}
        m = c.execute("SELECT repos,fetched_at FROM team_meta WHERE login=?", (login,)).fetchone()
    return days, (json.loads(m["repos"]) if m else []), (m["fetched_at"] if m else None)

def set_rock(task_key, rock_id, rationale):
    with conn() as c: c.execute("INSERT OR REPLACE INTO rock_map VALUES(?,?,?,?)", (task_key, rock_id, rationale, now()))

def rock_map():
    with conn() as c: return {r["task_key"]: dict(r) for r in c.execute("SELECT * FROM rock_map")}

# --- per-item to-do list. Open rows first in position order, done rows sink to the bottom (newest done last).
def todos(iid):
    with conn() as c: return rows(c.execute("SELECT * FROM item_todos WHERE item_id=? ORDER BY done, CASE WHEN done THEN done_at ELSE '' END, pos, created_at", (iid,)))

def todos_for(ids):
    if not ids: return {}
    out = {i: [] for i in ids}
    with conn() as c:
        for r in rows(c.execute(f"SELECT * FROM item_todos WHERE item_id IN ({','.join('?'*len(ids))}) ORDER BY done, CASE WHEN done THEN done_at ELSE '' END, pos, created_at", list(ids))):
            out.setdefault(r["item_id"], []).append(r)
    return out

def add_todos(iid, texts, source="me"):
    """Append one or many. Returns the new rows. Exact-duplicate open text is skipped so a session re-pushing a list does not double it."""
    texts = [t.strip() for t in (texts if isinstance(texts, (list, tuple)) else [texts]) if t and t.strip()]
    added = []
    with conn() as c:
        have = {r["text"] for r in c.execute("SELECT text FROM item_todos WHERE item_id=?", (iid,))}
        pos = (c.execute("SELECT COALESCE(MAX(pos),0) FROM item_todos WHERE item_id=?", (iid,)).fetchone()[0] or 0)
        for t in texts:
            if t in have: continue
            pos += 1; tid = new_id(); have.add(t)
            c.execute("INSERT INTO item_todos(id,item_id,text,done,pos,source,created_at,updated_at) VALUES(?,?,?,0,?,?,?,?)", (tid, iid, t[:500], pos, source, now(), now()))
            added.append(tid)
    return added

def update_todo(tid, **f):
    if "done" in f: f["done"] = 1 if f["done"] else 0; f["done_at"] = now() if f["done"] else None
    if "text" in f: f["text"] = (f["text"] or "").strip()[:500]
    f["updated_at"] = now()
    with conn() as c: c.execute(f"UPDATE item_todos SET {','.join(k+'=?' for k in f)} WHERE id=?", [*f.values(), tid])
    with conn() as c: r = c.execute("SELECT * FROM item_todos WHERE id=?", (tid,)).fetchone()
    return dict(r) if r else None

def delete_todo(tid):
    with conn() as c: c.execute("DELETE FROM item_todos WHERE id=?", (tid,))

def item_by_key_or_sid(key=None, sid=None):
    with conn() as c:
        if key:
            r = c.execute("SELECT * FROM items WHERE upper(shipyard_key)=? ORDER BY created_at DESC LIMIT 1", (key.upper(),)).fetchone()
            if r: return _load(dict(r))
        if sid:
            r = c.execute("SELECT * FROM items WHERE live_sid=? OR claude_session_id=? ORDER BY created_at DESC LIMIT 1", (sid, sid)).fetchone()
            if r: return _load(dict(r))
    return None

def migrate_v1():
    """Carry the two v1 `tasks` rows into `items` once."""
    with conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone(): return
        if c.execute("SELECT COUNT(*) FROM items").fetchone()[0]: return
        for t in rows(c.execute("SELECT * FROM tasks WHERE status NOT IN ('done','dropped')")):
            c.execute("INSERT INTO items(id,title,body,owner,status,assignee,requester,vip_tier,channel,source_type,permalink,shipyard_key,route,scores,autonomy_total,band,rationale,intake_event_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["title"], t.get("description") or "", "team" if t.get("route") == "team" else "me", "open", t.get("assignee"), t.get("requester"), t.get("vip_tier"), t.get("channel"), t.get("source_type"), t.get("permalink"), t.get("shipyard_key"), t.get("route"), t.get("scores") or "{}", t.get("autonomy_total"), t.get("band"), t.get("rationale") or "", t.get("intake_event_id"), t["created_at"], t["updated_at"]))


# --- session roster: every live Claude session the center has ever seen, so a dead tmux server or a killed
# Terminal loses nothing. ~/.claude/sessions/<pid>.json vanishes with the process; this table does not.
def upsert_roster(sessions):
    with conn() as c:
        for x in sessions:
            c.execute("""INSERT INTO session_roster(sid,cwd,title,pane,pid,kind,first_seen,last_seen) VALUES(?,?,?,?,?,?,?,?)
                         ON CONFLICT(sid) DO UPDATE SET cwd=COALESCE(excluded.cwd,cwd), title=COALESCE(excluded.title,title),
                         pane=excluded.pane, pid=excluded.pid, kind=COALESCE(excluded.kind,kind), last_seen=excluded.last_seen""",
                      (x["sid"], x.get("cwd"), x.get("title") or x.get("name"), x.get("pane"), x.get("pid"), x.get("kind"), now(), now()))

def roster():
    with conn() as c: return rows(c.execute("SELECT * FROM session_roster ORDER BY last_seen DESC"))


# --- EPD Rolling Roadmap rows. One row per project, keyed by its Shipyard key. Justin edits these in the
# team tab and the team edits the same rows in the claude.ai artifact; cc/epd.py reconciles the two.
ROADMAP_FIELDS = ("assignee", "phase", "delivery", "progress", "rank", "rock_id", "rock_note", "team_view",
                  "summary", "week_of", "start_date", "target_date", "title", "link", "link_kind", "tickets")

def _rm_load(r):
    if not r: return r
    r = dict(r)
    try: r["tickets"] = json.loads(r.get("tickets") or "[]")
    except Exception: r["tickets"] = []
    r["team_view"] = bool(r.get("team_view")); r["deleted"] = bool(r.get("deleted"))
    return r

def roadmap_rows(include_deleted=False):
    q = "SELECT * FROM roadmap" + ("" if include_deleted else " WHERE deleted=0")
    with conn() as c: return [_rm_load(r) for r in rows(c.execute(q + " ORDER BY rank IS NULL, rank, task_key"))]

def roadmap_row(key):
    with conn() as c: return _rm_load(c.execute("SELECT * FROM roadmap WHERE task_key=?", (key,)).fetchone())

def roadmap_upsert(key, by="justin", at=None, remote_at=None, **fields):
    """Write the fields given and leave the rest alone. `at` lets a pull stamp the remote row's own time."""
    vals = {k: v for k, v in fields.items() if k in ROADMAP_FIELDS}
    if "tickets" in vals and not isinstance(vals["tickets"], str): vals["tickets"] = json.dumps(vals["tickets"])
    if "team_view" in vals: vals["team_view"] = 1 if vals["team_view"] else 0
    stamp = at or now()
    with conn() as c:
        if not c.execute("SELECT 1 FROM roadmap WHERE task_key=?", (key,)).fetchone():
            c.execute("INSERT INTO roadmap(task_key,updated_at,updated_by) VALUES(?,?,?)", (key, stamp, by))
        if vals:
            c.execute(f"UPDATE roadmap SET {','.join(k + '=?' for k in vals)} WHERE task_key=?", (*vals.values(), key))
        c.execute("UPDATE roadmap SET updated_at=?, updated_by=?, deleted=0 WHERE task_key=?", (stamp, by, key))
        if remote_at: c.execute("UPDATE roadmap SET remote_at=? WHERE task_key=?", (remote_at, key))
    return roadmap_row(key)

def roadmap_delete(key, by="justin"):
    """Tombstone rather than drop, so the next sync can carry the removal up to the artifact."""
    with conn() as c:
        c.execute("UPDATE roadmap SET deleted=1, updated_at=?, updated_by=? WHERE task_key=?", (now(), by, key))

def roadmap_mark_synced(key, remote_at):
    with conn() as c: c.execute("UPDATE roadmap SET remote_at=? WHERE task_key=?", (remote_at, key))
