"""The EPD Rolling Roadmap, mirrored between the team tab and the claude.ai artifact.

Shipyard is the master for what a project IS — title, assignee, status, link. The PM fields on top of
that (phase, delivery, progress, rank, rock, team_view, week_of) have no home in Shipyard's API, so they
live in the local `roadmap` table keyed by the same Shipyard key.

The artifact's `projects` collection holds exactly the same rows. This server has no credentials to
claude.ai, so it cannot write that collection itself — a Claude run does, every 15 minutes, by calling
`export()` here for what we believe and `merge_remote()` with what the artifact says. Reconciliation is
last-write-wins per row on `updated_at`, which both sides stamp.
"""
import json
from . import db, shipyard
from .config import TEAM, ME, ME_UID, SHIPYARD_HOST

# The artifact writes an assignee slug, not a GitHub login. Keep the two vocabularies pinned together
# here so neither side has to guess.
SLUG_BY_LOGIN = {login: login.split("-")[0] for login in TEAM}
LOGIN_BY_SLUG = {v: k for k, v in SLUG_BY_LOGIN.items()}
SLUG_BY_UID = {uid: SLUG_BY_LOGIN[login] for login, (_n, uid, _s) in TEAM.items()}
SLUG_BY_UID[ME_UID] = "justin"
LOGIN_BY_SLUG["justin"] = ME

PHASES = ("Discovery & Definition", "Development", "Evaluation", "Launch", "Complete")
DELIVERY = ("Not Started", "On Time", "At Risk", "Late", "Shipped")
PHASE_BY_STATUS = {"backlog": "Discovery & Definition", "todo": "Discovery & Definition",
                   "in_progress": "Development", "in_review": "Evaluation", "done": "Complete"}


def _link(key): return f"https://{SHIPYARD_HOST}/t/{key}"


def refresh_from_shipyard():
    """Pull every open team task and make sure each one has a roadmap row. Only fills fields the row does
    not already carry — a phase Justin or the team set by hand is never overwritten by the ticket status."""
    seen = []
    for uid in list(SLUG_BY_UID):
        try: tasks = shipyard.open_tasks(assignee_uid=uid)
        except Exception: continue
        for t in tasks:
            key = t.get("key")
            if not key or key in seen: continue
            seen.append(key)
            cur = db.roadmap_row(key)
            fields = {"title": t.get("title"), "link": _link(key), "link_kind": "ticket",
                      "assignee": SLUG_BY_UID[uid], "tickets": [key]}
            if not cur:
                fields |= {"phase": PHASE_BY_STATUS.get(t.get("status")), "delivery": "Not Started",
                           "progress": shipyard.PROGRESS.get(t.get("status"), 0), "team_view": True,
                           "rock_id": (db.rock_map().get(key) or {}).get("rock_id")}
            db.roadmap_upsert(key, by="shipyard", **fields)
    return seen


def rows(team_view_only=False):
    out = [r for r in db.roadmap_rows() if not team_view_only or r["team_view"]]
    return out


def as_artifact(r):
    """One local row in the shape the artifact's `projects` collection uses."""
    return {"assignee": r.get("assignee"), "phase": r.get("phase"), "delivery": r.get("delivery"),
            "progress": r.get("progress"), "rank": r.get("rank"), "rock_id": r.get("rock_id"),
            "rock_note": r.get("rock_note") or "", "team_view": bool(r.get("team_view")),
            "summary": r.get("summary") or "", "week_of": r.get("week_of"),
            "start_date": r.get("start_date"), "target_date": r.get("target_date"),
            "title": r.get("title"), "link": r.get("link"), "link_kind": r.get("link_kind") or "ticket",
            "tickets": r.get("tickets") or [r["task_key"]],
            "updated_at": r.get("updated_at"), "updated_by": r.get("updated_by") or "justin"}


def export():
    """What this side believes, for the sync to compare against the artifact."""
    live = [r for r in db.roadmap_rows(include_deleted=True) if not r["deleted"]]
    gone = [r["task_key"] for r in db.roadmap_rows(include_deleted=True) if r["deleted"]]
    return {"projects": {r["task_key"]: as_artifact(r) for r in live}, "deleted": gone,
            "synced": {r["task_key"]: r.get("remote_at") for r in live}}


def merge_remote(remote):
    """Take the artifact's `projects` rows and reconcile. `remote` is {key: {..., updated_at, updated_by}}.

    A remote row wins when its `updated_at` is newer than ours; otherwise ours stands and the sync pushes
    it back up. Returns what the caller must now write to the artifact."""
    push, pulled, conflicts = {}, [], []
    for key, rem in (remote or {}).items():
        if not isinstance(rem, dict): continue
        cur = db.roadmap_row(key)
        r_at = (rem.get("updated_at") or "")[:19]
        if cur and cur.get("deleted"):
            continue                       # we tombstoned it; the sync deletes it up there
        if not cur:
            db.roadmap_upsert(key, by=rem.get("updated_by") or "team", at=rem.get("updated_at"),
                              remote_at=r_at, **{k: rem.get(k) for k in db.ROADMAP_FIELDS if k in rem})
            pulled.append(key); continue
        l_at = (cur.get("updated_at") or "")[:19]
        if r_at > l_at:
            db.roadmap_upsert(key, by=rem.get("updated_by") or "team", at=rem.get("updated_at"),
                              remote_at=r_at, **{k: rem.get(k) for k in db.ROADMAP_FIELDS if k in rem})
            pulled.append(key)
        elif l_at > r_at:
            push[key] = as_artifact(cur)
            if r_at and r_at != (cur.get("remote_at") or ""): conflicts.append(key)
        else:
            db.roadmap_mark_synced(key, r_at)
    # anything we hold that the artifact has never seen
    for r in db.roadmap_rows():
        if r["task_key"] not in (remote or {}): push[r["task_key"]] = as_artifact(r)
    delete = [r["task_key"] for r in db.roadmap_rows(include_deleted=True)
              if r["deleted"] and r["task_key"] in (remote or {})]
    return {"push": push, "delete": delete, "pulled": pulled, "conflicts": conflicts}


def top_three():
    """The front-page table: each owner's three ranked projects."""
    by = {}
    for r in rows(team_view_only=True):
        if r.get("rank") in (1, 2, 3): by.setdefault(r["assignee"] or "unassigned", []).append(r)
    return {k: sorted(v, key=lambda r: r["rank"])[:3] for k, v in by.items()}
