"""The team tab's Update button, at the level Justin plans at: the project, not the ticket.

A project is one row on the EPD Rolling Roadmap -- the PRD and every Shipyard ticket hanging
off it. The board already groups them that way (roadmap.py sync: tasks sharing a PRD doc become
one row), so this reads the same rows and asks one question of each:

    has every pull request under this project been merged, and has it been quiet for a week?

If yes the project is finished, full stop -- no list to review. The work is: mark the roadmap row
Done, mark its Shipyard tickets done, and record the day the last pull request merged, which is the
day the project actually shipped. That ship date is what the per-person chart counts.

LaunchDarkly is the one veto. Merged is not shipped: a project's four pull requests can all be merged
while opp-gen-auto-territory-assignment-mode still serves dry_run in production. A flag is tied to
a project when the flag's name, description or tags carry one of its ticket keys, or when the flag
key appears in the body of one of its pull requests (how opp-generation #2374 names the flag it
added). A project whose flag is off or in dry run is held open and labelled, never closed.

Everything it does is reversible: the roadmap row goes back with `roadmap.py set <id> phase=...`
and every ticket carries a note saying what moved it and how to put it back.
"""
import json, os, re, subprocess, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

from . import db, epd, github, shipyard
from .config import TEAM, ME_UID, TICKET_PREFIX, LD_PROJECT, LD_ENV, LD_TAG

LD_HOST = "https://app.launchdarkly.com"
QUIET_DAYS = 7                  # merged and then this quiet = nobody is still working on it
KEY_RE = re.compile(rf"{re.escape(TICKET_PREFIX)}-(\d+)", re.I)
SLUG_RE = re.compile(r"[a-z0-9]+(?:[-.][a-z0-9]+){2,}")
ACTIVE_TICKET = ("todo", "in_progress", "in_review")

# ---------------------------------------------------------------- LaunchDarkly

def ld_token():
    """A LaunchDarkly read token, from the environment or ~/.ld_env. The server Claude talks to signs
    in per session over OAuth, which a long-running local server cannot do, so the flag read needs its
    own token. Without one the audit still runs and says the flag check was skipped."""
    t = (os.environ.get("LD_API_TOKEN") or os.environ.get("LAUNCHDARKLY_API_KEY") or "").strip()
    if t: return t
    p = os.path.expanduser("~/.ld_env")
    if os.path.exists(p):
        for line in open(p):
            m = re.match(r"\s*(?:export\s+)?(?:LD_API_TOKEN|LAUNCHDARKLY_API_KEY)\s*=\s*['\"]?([^'\"\s]+)", line)
            if m: return m.group(1)
    return ""

def _served(flag):
    """What production actually serves. Off serves the off variation; on with no rules serves the
    fallthrough. Rules or a percentage rollout mean several answers at once, so say so, do not guess."""
    env = (flag.get("environments") or {}).get(LD_ENV) or {}
    var = flag.get("variations") or []
    def val(i):
        try: return var[i].get("value")
        except Exception: return None
    if not env.get("on"): return val(env.get("offVariation", 0)), "off"
    ft = env.get("fallthrough") or {}
    if ft.get("rollout"): return None, "rollout"
    if env.get("rules"): return val(ft.get("variation", 0)), "rules"
    return val(ft.get("variation", 0)), "fallthrough"

def _mode(value, how):
    if how == "rollout": return "partial"
    s = str(value).lower()
    if value is False or s in ("off", "none", "disabled"): return "off"
    if "dry" in s or "shadow" in s or "plan" in s: return "dry run"
    if how == "rules": return "live for some"
    return "live"

def ld_flags():
    """Every GTM Products flag and what production serves. ([], reason) when there is no token."""
    tok = ld_token()
    if not tok: return [], "no token"
    url = f"{LD_HOST}/api/v2/flags/{LD_PROJECT}?env={LD_ENV}&summary=false&tag={LD_TAG}&limit=100"
    req = urllib.request.Request(url, headers={"Authorization": tok, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r: d = json.load(r)
    except urllib.error.HTTPError as e: return [], f"http {e.code}"
    except Exception as e: return [], str(e)[:80]
    out = []
    for f in d.get("items", []):
        value, how = _served(f)
        text = " ".join([f.get("key") or "", f.get("name") or "", f.get("description") or "", *(f.get("tags") or [])])
        out.append({"key": f.get("key"), "name": f.get("name") or f.get("key"), "mode": _mode(value, how),
                    "serving": "" if value is None else str(value), "how": how,
                    "url": f"{LD_HOST}/{LD_PROJECT}/{LD_ENV}/features/{f.get('key')}",
                    "tickets": sorted({f"{TICKET_PREFIX}-" + n for n in KEY_RE.findall(text)})})
    return out, "ok"

# ---------------------------------------------------------------- pull requests

def _pr_cache():
    try: return json.loads(db.get_setting("audit.prs") or "{}")
    except Exception: return {}

def pr_state(url, cache):
    """Merged, open or closed-without-merging, plus every hyphenated slug in the body -- that is where
    a pull request that hides its change behind a flag names the flag. A merged PR can never change
    again, so its record is cached forever and never re-read."""
    hit = cache.get(url)
    if hit and hit.get("merged") and "slugs" in hit: return hit
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url or "")
    if not m: return {"state": "?", "merged": False, "merged_at": "", "slugs": []}
    o, r, n = m.groups()
    try:
        q = subprocess.run(["gh", "api", f"repos/{o}/{r}/pulls/{n}", "--jq",
                            '[.state,(.merged|tostring),(.merged_at//""),(.body//"")]|@tsv'],
                           capture_output=True, text=True, timeout=30)
        state, merged, at, body = (q.stdout.strip().split("\t") + ["", "", "", ""])[:4]
        rec = {"state": state, "merged": merged == "true", "merged_at": at, "repo": r, "number": int(n),
               "slugs": sorted(set(SLUG_RE.findall(body.lower())))[:200]}
    except Exception as e:
        rec = {"state": "unknown", "merged": False, "merged_at": "", "slugs": [], "error": str(e)[:60]}
    cache[url] = rec
    return rec

def _age_days(iso):
    try: return (datetime.now(timezone.utc) - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days
    except Exception: return None

# ---------------------------------------------------------------- one project

def assess(proj, cache, flags):
    """What the code says about one roadmap project, independent of the phase a human last typed."""
    keys = proj.get("tickets") or ([proj["id"]] if re.match(rf"^{re.escape(TICKET_PREFIX)}-\d+$", proj.get("id") or "") else [])
    prs, tickets, named = [], [], set()
    for k in keys:
        t = shipyard.call("get_task", task_id=k)
        if not isinstance(t, dict) or t.get("error"): continue
        tickets.append({"key": k, "status": t.get("status"), "title": t.get("title") or "",
                        "type": t.get("type") or "task",
                        "reporter": ((t.get("reporter") or {}).get("uid") or t.get("reporter_uid") or "")})
        for p in ((t.get("metadata") or {}).get("prs") or []):
            if not p.get("url"): continue
            st = pr_state(p["url"], cache); named |= set(st.get("slugs") or [])
            prs.append({"url": p["url"], "ticket": k, **st})
    mine = [f for f in flags
            if f.get("key") in named or any(k in (f.get("tickets") or []) for k in keys)]
    stuck = [f for f in mine if f["mode"] in ("off", "dry run")]
    merged = [p for p in prs if p.get("merged")]
    open_prs = [p for p in prs if not p.get("merged") and p.get("state") == "open"]
    newest = max((p.get("merged_at") or "" for p in merged), default="")
    age = _age_days(newest) if newest else None
    base = {"id": proj.get("id"), "title": proj.get("title") or "", "assignee": proj.get("assignee"),
            "phase": proj.get("phase"), "link": proj.get("link"), "link_kind": proj.get("link_kind"),
            "tickets": tickets, "prs": len(prs), "merged": len(merged), "open_prs": len(open_prs),
            "shipped_at": newest[:10] if newest else "", "age_days": age,
            "flags": [{"key": f["key"], "mode": f["mode"], "url": f["url"], "serving": f["serving"]} for f in mine],
            "from_me": any(t["reporter"] == ME_UID for t in tickets)}
    if not prs:       return {**base, "state": "no code yet"}
    if open_prs:      return {**base, "state": "in flight"}
    if not merged:    return {**base, "state": "abandoned"}
    if stuck:         return {**base, "state": "held by flag",
                              "why": ", ".join(f"{f['key']} serves {f['serving'] or f['mode']}" for f in stuck)}
    if age is None or age < QUIET_DAYS:
        return {**base, "state": "just merged", "why": f"newest merge {newest[:10]}, under {QUIET_DAYS} days old"}
    return {**base, "state": "shipped", "why": f"all {len(merged)} pull requests merged, newest {newest[:10]} ({age}d ago)"}

# ---------------------------------------------------------------- closing

NOTE = ("Closed automatically on {today} by the command center's team update. Every pull request under "
        "{pid} ({title}) is merged, the newest on {shipped} ({age} days ago), and none is still open, so "
        "the project is treated as shipped. If this is wrong, set the ticket back to {was} and it is as it was.")

def close(a, pushed):
    """Mark one project shipped: the roadmap row Done, every open ticket under it done with a note."""
    out = {"id": a["id"], "title": a["title"], "assignee": a["assignee"], "shipped_at": a["shipped_at"], "tickets": []}
    try:
        epd.cli("finish", a["id"])
        pushed.append(a["id"])
    except Exception as e:
        out["error"] = f"roadmap: {str(e)[:120]}"
    for t in a["tickets"]:
        if t["status"] not in ACTIVE_TICKET: continue
        # Shipyard refuses to close a bug without saying how it ended, and the pull requests merged,
        # so the honest answer is that code fixed it.
        args = {"task_id": t["key"], "status": "done"}
        if t.get("type") == "bug": args["resolution"] = "fixed_code"
        try:
            shipyard.call("add_task_note", task_id=t["key"], content=NOTE.format(
                today=datetime.now().date(), pid=a["id"], title=a["title"][:60],
                shipped=a["shipped_at"], age=a["age_days"], was=t["status"]))
            r = shipyard.call("update_task", **args)
            # shipyard.call hands back the server's error object rather than raising, so a silent
            # refusal here would otherwise be reported as a successful close.
            if isinstance(r, dict) and r.get("error"):
                raise RuntimeError(str(r["error"])[:120])
            out["tickets"].append(t["key"])
        except Exception as e:
            out.setdefault("ticket_errors", []).append(f"{t['key']}: {str(e)[:100]}")
    return out

# ---------------------------------------------------------------- the run

def ship_log():
    """Every project this has closed, and the day it shipped. Kept locally because the roadmap drops
    its own Done rows after 30 days and the chart looks back 14."""
    try: return json.loads(db.get_setting("audit.shipped") or "{}")
    except Exception: return {}

def run():
    started = db.now()
    try: github.refresh_team()
    except Exception as e: print("audit: github refresh failed:", e)
    try: epd.pull()
    except Exception as e: print("audit: roadmap pull failed:", e)
    flags, ld_status = ld_flags()
    if flags:
        db.set_setting("audit.flags", json.dumps({"as_of": db.now(), "flags": flags}))
    else:
        # No token today. Rather than close a project whose switch we know is off, fall back to the last
        # flag states we did read. Stale, and labelled stale, but a stale veto beats a wrong close.
        try: snap = json.loads(db.get_setting("audit.flags") or "{}")
        except Exception: snap = {}
        if snap.get("flags"):
            flags = snap["flags"]
            ld_status = f"{ld_status}, using the {snap.get('as_of', '')[:10]} snapshot of {len(flags)} flag(s)"
    cache, log = _pr_cache(), ship_log()
    rows, closed, pushed = [], [], []
    for p in epd.board().get("projects", []):
        if p.get("phase") == "Done":
            if p.get("id") not in log and p.get("done_at"):
                log[p["id"]] = {"assignee": p.get("assignee"), "day": p["done_at"][:10], "title": p.get("title") or ""}
            continue
        a = assess(p, cache, flags)
        if a["state"] == "shipped":
            c = close(a, pushed); closed.append(c)
            log[a["id"]] = {"assignee": a["assignee"], "day": a["shipped_at"], "title": a["title"]}
            a["phase"] = "Done"
        rows.append(a)
    if pushed:
        try: epd.cli("push")
        except Exception as e: print("audit: roadmap push failed:", e)
    db.set_setting("audit.prs", json.dumps(cache))
    db.set_setting("audit.shipped", json.dumps(log))
    blob = {"at": db.now(), "started": started, "projects_checked": len(rows), "ld": ld_status,
            "closed": closed, "rows": rows,
            "flags_not_live": [f for f in flags if f["mode"] in ("off", "dry run")]}
    db.set_setting("audit.last", json.dumps(blob))
    return blob

def last():
    try: return json.loads(db.get_setting("audit.last") or "{}")
    except Exception: return {}

def shipped_series(assignee, days=14):
    """Projects shipped per day for one person, newest day last. The ship day is the day the project's
    last pull request merged, not the day somebody typed Done on the board."""
    log = ship_log()
    per = {}
    for v in log.values():
        if v.get("assignee") != assignee or not v.get("day"): continue
        per[v["day"]] = per.get(v["day"], 0) + 1
    today = datetime.now().date()
    out = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        out.append({"day": d.isoformat(), "dow": d.strftime("%a"), "shipped": per.get(d.isoformat(), 0)})
    monday = today - timedelta(days=today.weekday())
    week = sum(v for k, v in per.items() if k >= monday.isoformat())
    return out, week, sum(v for k, v in per.items() if k >= (today - timedelta(days=days)).isoformat())
