"""gh CLI wrappers. gh reads its own keyring. Team activity is commits per day, not open PRs."""
import json, subprocess, time
from datetime import datetime, timedelta
from .config import GH_ORG, ME, TEAM
from . import db

def _gh(args, timeout=60):
    r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0: raise RuntimeError(r.stderr.strip()[:300])
    return json.loads(r.stdout or "[]")

def my_open_prs():
    items = _gh(["search", "prs", "--author", ME, "--state", "open", "--owner", GH_ORG,
                 "--json", "number,title,repository,url,updatedAt,isDraft", "--limit", "50"])
    out = [{"url": p["url"], "repo": p["repository"]["name"], "number": p["number"], "title": p["title"], "author": ME,
            "draft": p["isDraft"], "state": "open", "updated_at": p["updatedAt"]} for p in items]
    db.upsert_prs(out)
    # anything we cached as open that GitHub no longer lists is merged or closed; drop it from the panel
    live_urls = {p["url"] for p in out}
    with db.conn() as c:
        c.execute("UPDATE prs SET state='closed' WHERE author=? AND state='open' AND url NOT IN (%s)" % ",".join("?" * len(live_urls)) if live_urls else "UPDATE prs SET state='closed' WHERE author=? AND state='open'", [ME, *live_urls])
    return out

def enrich_pr(url):
    try:
        q = ["gh", "pr", "view", url, "--json", "reviewDecision,statusCheckRollup,mergeStateStatus,autoMergeRequest,state,isDraft,title"]
        d = json.loads(subprocess.run(q, capture_output=True, text=True, timeout=30).stdout)
        if d.get("mergeStateStatus") == "UNKNOWN":   # GitHub computes mergeability lazily; the first read often says UNKNOWN
            time.sleep(1.2); d = json.loads(subprocess.run(q, capture_output=True, text=True, timeout=30).stdout)
        states = [c.get("state") or c.get("conclusion") for c in d.get("statusCheckRollup") or []]
        bad = [s for s in states if s and s not in ("SUCCESS", "SKIPPED", "NEUTRAL", "PENDING", "IN_PROGRESS", "QUEUED")]
        pend = [s for s in states if s in ("PENDING", "IN_PROGRESS", "QUEUED", None)]
        ci = "failing" if bad else "pending" if pend else "green"
        return {"review": d.get("reviewDecision") or "", "ci": ci, "merge": d.get("mergeStateStatus"), "auto": bool(d.get("autoMergeRequest")),
                "state": d.get("state") or "", "draft": bool(d.get("isDraft")), "title": d.get("title") or ""}
    except Exception:
        return {"review": "", "ci": "unknown", "state": "", "draft": False, "title": ""}

def commits_by_day(login, days=14):
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    items = _gh(["search", "commits", "--author", login, "--owner", GH_ORG, "--author-date", f">={since}",
                 "--json", "commit,repository", "--limit", "100"])
    day, repos = {}, {}
    for c in items:
        d = (c["commit"]["author"]["date"] or "")[:10]; day[d] = day.get(d, 0) + 1
        repos[c["repository"]["name"]] = repos.get(c["repository"]["name"], 0) + 1
    return day, sorted(repos.items(), key=lambda x: -x[1])

def refresh_team():
    for login in TEAM:
        try:
            day, repos = commits_by_day(login); db.set_daily(login, day, repos)
        except Exception as e:
            _, repos, _ = db.get_daily(login); db.set_daily(login, dict(db.get_daily(login)[0]), repos + [["stale", str(e)[:40]]])
        time.sleep(2)  # search API: 30 req/min shared bucket

def team_card(login, days=14):
    name = TEAM[login][0]; day, repos, fetched = db.get_daily(login)
    today = datetime.now().date(); series = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)); series.append({"day": d.isoformat(), "dow": d.strftime("%a"), "commits": day.get(d.isoformat(), 0)})
    monday = today - timedelta(days=today.weekday())
    week = sum(v for k, v in day.items() if k >= monday.isoformat())
    return {"login": login, "name": name, "series": series, "today": day.get(today.isoformat(), 0), "week": week,
            "avg_per_workday": round(sum(day.values()) / 10, 1), "repos": [r for r in repos if r[0] != "stale"][:4], "fetched_at": fetched}
