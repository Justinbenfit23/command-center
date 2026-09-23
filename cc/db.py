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

def migrate_v1():
    """Carry the two v1 `tasks` rows into `items` once."""
    with conn() as c:
        if not c.execute("SELECT name FROM sqlite_master WHERE name='tasks'").fetchone(): return
        if c.execute("SELECT COUNT(*) FROM items").fetchone()[0]: return
        for t in rows(c.execute("SELECT * FROM tasks WHERE status NOT IN ('done','dropped')")):
            c.execute("INSERT INTO items(id,title,body,owner,status,assignee,requester,vip_tier,channel,source_type,permalink,shipyard_key,route,scores,autonomy_total,band,rationale,intake_event_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (t["id"], t["title"], t.get("description") or "", "team" if t.get("route") == "team" else "me", "open", t.get("assignee"), t.get("requester"), t.get("vip_tier"), t.get("channel"), t.get("source_type"), t.get("permalink"), t.get("shipyard_key"), t.get("route"), t.get("scores") or "{}", t.get("autonomy_total"), t.get("band"), t.get("rationale") or "", t.get("intake_event_id"), t["created_at"], t["updated_at"]))
