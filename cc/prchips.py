"""Live status for the pull-request links pinned on cards (items.links, kind "pr"). One background thread
re-reads every PR on an open card about once a minute through `gh pr view`; the card polls read this cache
and never wait on gh. A PR on an archived card is refreshed while someone is looking at it."""
import re, threading, time
from . import db, github
from .config import GH_ORG, ME

PR = re.compile(rf"https://github\.com/{re.escape(GH_ORG)}/([\w.-]+)/pull/(\d+)")
PERIOD = 60          # seconds between sweeps
GAP = 0.3            # seconds between gh calls inside a sweep
WANT_FOR = 600       # a url asked for by a card poll stays in the sweep this long after the last ask
_cache, _wanted, _inflight = {}, {}, set()
_lock = threading.Lock()

def _soon(url):
    """First look at a PR nobody has read yet (an archived card just opened): read it now, once, off-thread."""
    with _lock:
        if url in _cache or url in _inflight: return
        _inflight.add(url)
    def go():
        try: refresh(url)
        except Exception: pass
        finally:
            with _lock: _inflight.discard(url)
    threading.Thread(target=go, daemon=True).start()

def chips_for(item):
    """The card's PR chips: repo and number from the url, everything else from the cache (ci "unknown" until
    the first sweep reaches it)."""
    out = []
    for l in item.get("links") or []:
        if l.get("kind") != "pr": continue
        m = PR.match(l["url"])
        if not m: continue
        with _lock:
            d = dict(_cache.get(l["url"]) or {"ci": "unknown", "review": "", "state": ""})
            _wanted[l["url"]] = time.time()
        if "at" not in d: _soon(l["url"])
        out.append({"url": l["url"], "repo": m.group(1), "number": int(m.group(2)), **d})
    return out

FRESH = 120          # a cache entry younger than this is served as-is by view()

def view(url):
    """Status for one PR: the cache when it is fresh, else a read now (cached for everyone after)."""
    with _lock: d = _cache.get(url)
    if d and time.time() - d["at"] < FRESH: return d
    return refresh(url)

def refresh(url):
    d = github.enrich_pr(url); d["at"] = time.time()
    with _lock: _cache[url] = d
    return d

def _urls():
    now = time.time()
    with _lock:
        for u, t in list(_wanted.items()):
            if now - t > WANT_FOR: del _wanted[u]
        want = set(_wanted)
    for it in db.list_items("open"):
        want.update(l["url"] for l in (it.get("links") or []) if l.get("kind") == "pr" and PR.match(l["url"]))
    try: want.update(p["url"] for p in db.prs_by_author(ME))   # the My PRs list reads the same cache
    except Exception: pass
    return sorted(want)

def _loop():
    while True:
        try:
            for u in _urls():
                try: refresh(u)
                except Exception: pass
                time.sleep(GAP)
        except Exception as e:
            print(f"prchips: {e}", flush=True)
        time.sleep(PERIOD)

def start():
    threading.Thread(target=_loop, daemon=True).start()
