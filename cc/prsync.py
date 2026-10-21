"""The card's "sync PRs" button. The poll pins only full GitHub URLs from the last 40 messages; a chat that
says "Radar #1449" or "#2588 is clean" pins nothing. This re-reads the WHOLE transcript, both sides, resolves
bare numbers against a repo and verifies each one with `gh pr view` before it lands on the card."""
import json, os, re, subprocess, time
from . import db, live, prchips, claude_sessions as cs
from .config import GH_ORG, ME

BARE = re.compile(r"(?:\b([A-Za-z][\w.-]{1,40})\s+)?(?<![\w/#])#(\d{2,6})\b")
MIN_BARE = 3   # "#15" with no repo named is noise; "radar #15" is a claim
WORDY = re.compile(r"\b(?:PR|pull request)\s+#?(\d{2,6})\b", re.I)
MAX_CHECKS = 15
_no = set()   # (repo, number) pairs GitHub said do not exist; not re-asked this process

def _full_text(sid):
    p = live._transcript_path(sid)
    if not p: return ""
    out = []
    with open(p, errors="replace") as f:
        for l in f:
            try: d = json.loads(l)
            except Exception: continue
            if d.get("type") not in ("user", "assistant") or d.get("isMeta"): continue
            out.append(live._text_of((d.get("message") or {}).get("content")) or "")
    return "\n".join(out)

def _known_repos():
    repos = set()
    for it in db.list_items("all"):
        for l in it.get("links") or []:
            m = prchips.PR.match(l.get("url", "")) if l.get("kind") == "pr" else None
            if m: repos.add(m.group(1).lower())
    try:
        with db.conn() as c: repos.update(r["repo"].lower() for r in db.rows(c.execute("SELECT DISTINCT repo FROM prs")) if r.get("repo"))
    except Exception: pass
    return repos

def _verify(repo, num, strict=False, key=None):
    """The PR exists. With `strict` (the repo was guessed, not named next to the number) it must also look like
    ours: authored by me, or its title/body carries the card's ticket key. A big repo has a PR for almost any
    number, so a guessed repo plus a bare number is otherwise a coin toss."""
    if (repo, num) in _no: return None
    r = subprocess.run(["gh", "pr", "view", str(num), "-R", f"{GH_ORG}/{repo}", "--json", "url,author,title,body"], capture_output=True, text=True, timeout=30)
    if r.returncode != 0 or not r.stdout.strip(): _no.add((repo, num)); return None
    try: d = json.loads(r.stdout)
    except Exception: return None
    if strict:
        mine = ((d.get("author") or {}).get("login") or "").lower() == ME.lower()
        keyed = bool(key) and key.upper() in ((d.get("title") or "") + " " + (d.get("body") or "")).upper()
        if not (mine or keyed): _no.add((repo, num)); return None
    return d["url"]

def sync(item):
    sid = item.get("live_sid")
    text = _full_text(sid) if sid else ""
    links = item.get("links") or []
    have = {l["url"] for l in links}
    found = []
    for u in cs.PR.findall(text):
        if u not in have: links.append({"kind": "pr", "url": u}); have.add(u); found.append(u)
    # bare numbers: a repo named just before ("Radar #1449"), else the repos this chat already linked, else the card's cwd
    in_chat = []
    for u in cs.PR.findall(text):
        m = prchips.PR.match(u)
        if m and m.group(1).lower() not in in_chat: in_chat.append(m.group(1).lower())
    cwd_repo = os.path.basename((item.get("cwd") or "").rstrip("/")).lower()
    known = _known_repos()
    pinned_nums = {(prchips.PR.match(l["url"]).group(1).lower(), int(prchips.PR.match(l["url"]).group(2))) for l in links if l.get("kind") == "pr" and prchips.PR.match(l["url"])}
    cands, seen = [], set()
    for hint, num in [(h, int(n)) for h, n in BARE.findall(text)] + [(None, int(n)) for n in WORDY.findall(text)]:
        hint = (hint or "").lower().replace("_", "-")
        named = hint in known
        if named:
            repos = [(hint, False)]                       # "Radar #1449": the chat said which repo
        else:
            if num < 10 ** (MIN_BARE - 1): continue
            if any(n == num for _, n in pinned_nums): continue   # already pinned under some repo; a twin elsewhere is a coincidence
            repos = [(r, True) for r in in_chat] + ([(cwd_repo, True)] if cwd_repo and cwd_repo in known and cwd_repo not in in_chat else [])
        for r, strict in repos:
            if (r, num) in pinned_nums or (r, num) in seen: continue
            seen.add((r, num)); cands.append((r, num, strict))
    checks = 0
    for r, num, strict in cands:
        if checks >= MAX_CHECKS: break
        if (r, num) in _no: continue
        checks += 1
        u = _verify(r, num, strict=strict, key=item.get("shipyard_key"))
        if u and u not in have:
            links.append({"kind": "pr", "url": u}); have.add(u); found.append(u); pinned_nums.add((r, num))
    if found: db.update_item(item["id"], links=json.dumps(links))
    for u in found:
        try: prchips.refresh(u)
        except Exception: pass
    fresh = db.get_item(item["id"])
    return {"added": found, "checked": checks, "candidates": len(cands), "prs": prchips.chips_for(fresh)}
