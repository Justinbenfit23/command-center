"""Which real model each picker alias ("fable"/"opus"/"sonnet") runs right now, read from what sessions
actually answered with. Every assistant line in a transcript names the model id that produced it
(e.g. claude-opus-5-5), so the newest id seen per family IS what the alias resolves to. When a new release
lands, the first session that uses it changes the label; nothing here is hard-coded to a version."""
import glob, os, re, time
from .live import PROJ, _transcript_path

FAMILIES = ("fable", "opus", "sonnet")
_ID = re.compile(r'"model":"(claude-(fable|opus|sonnet)-[0-9][0-9a-z-]*)"')
_cache = {"at": 0.0, "v": {}}

def _version(mid):
    """claude-opus-5-5 -> (5, 5); claude-opus-5 -> (5, 0); date suffixes (8 digits) are dropped."""
    nums = [int(x) for x in mid.split("-")[2:] if x.isdigit() and len(x) < 4]
    return tuple(nums + [0] * (2 - len(nums)))[:2]

def label(mid):
    if not mid: return None
    fam = mid.split("-")[1]; v = _version(mid)
    return f"{fam.capitalize()} {v[0]}.{v[1]}" if v[1] else f"{fam.capitalize()} {v[0]}"

def _ids_in(path, tail=200_000):
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.path.getsize(path) - tail)); txt = f.read().decode("utf-8", "replace")
    except OSError: return []
    return [m.group(1) for m in _ID.finditer(txt)]

def newest(max_age=600):
    """{family: {"id", "label"}} for the newest model seen in the 80 most recently written transcripts."""
    if time.time() - _cache["at"] < max_age and _cache["v"]: return _cache["v"]
    files = sorted(glob.glob(os.path.join(PROJ, "*", "*.jsonl")), key=os.path.getmtime, reverse=True)[:80]
    best = {}
    for p in files:
        for mid in set(_ids_in(p)):
            fam = mid.split("-")[1]
            if fam not in best or _version(mid) > _version(best[fam]): best[fam] = mid
    _cache.update(at=time.time(), v={f: {"id": best.get(f), "label": label(best.get(f)) or f"{f.capitalize()} (version not seen yet)"} for f in FAMILIES})
    return _cache["v"]

def of_session(sid):
    """The model id that answered last in this session, or None."""
    p = _transcript_path(sid) if sid else None
    ids = _ids_in(p, 60_000) if p else []
    return ids[-1] if ids else None
