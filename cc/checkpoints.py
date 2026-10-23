"""Daily checkpoint strip. Calendar events whose title contains "checkpoint" ("<KEY> checkpoint: ...") are read
once a morning, and on demand, through CHECKPOINTS_CMD from config (any command that prints one event per line as
"<KEY> <title> <url>"). CHECKPOINTS_CMD = None disables the strip. Nothing here writes to the calendar."""
import os, re, subprocess, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import CHECKPOINTS_CMD, TIMEZONE
TZ = ZoneInfo(TIMEZONE)
LINE = re.compile(r"^(\S+)\s+(.*?)\s+(https://www\.google\.com/calendar/\S+)\s*$")
KEY = re.compile(r"^([A-Z][A-Z0-9]+-\d+)\b")
_lock = threading.Lock()
_cache = {"date": None, "at": 0, "events": [], "error": None}
_ran = {}   # event url -> local time the run line went into a session (this server's lifetime only)

def today(): return datetime.now(TZ).date().isoformat()

def fetch(day):
    """Events on `day` with "checkpoint" in the title. --list prints the neighbouring days too (its window is
    UTC), so each line is re-checked against the Denver date."""
    if not CHECKPOINTS_CMD: raise RuntimeError("checkpoint strip is off: set CHECKPOINTS_CMD in cc/config.py")
    r = subprocess.run([*CHECKPOINTS_CMD, day], capture_output=True, text=True, timeout=60)
    if r.returncode != 0: raise RuntimeError((r.stderr or r.stdout).strip()[-300:] or "checkpoint command failed")
    out = []
    for line in r.stdout.splitlines():
        m = LINE.match(line.strip())
        if not m: continue
        when, title, url = m.groups()
        if "checkpoint" not in title.lower(): continue
        if "T" in when:
            try: dt = datetime.fromisoformat(when).astimezone(TZ)
            except Exception: continue
            if dt.date().isoformat() != day: continue
            start, t = dt.isoformat(), dt.strftime("%H:%M")
        else:
            if when != day: continue
            start, t = when + "T00:00", "all day"
        k = KEY.match(title)
        out.append({"start": start, "time": t, "title": title, "key": k.group(1) if k else None, "url": url})
    out.sort(key=lambda e: e["start"])
    return out

def get(refresh=False, day=None):
    """Today's strip from the cache, re-read on the first call of a new day or when asked. A `day` other than
    today is read live and never cached (that path exists for testing)."""
    if day and day != today():
        try: return {"date": day, "fetched_at": time.time(), "events": fetch(day), "error": None, "cached": False}
        except Exception as e: return {"date": day, "fetched_at": time.time(), "events": [], "error": str(e)[:300], "cached": False}
    day = today()
    with _lock:
        if _cache["date"] != day or refresh:
            try: _cache.update(date=day, at=time.time(), events=fetch(day), error=None)
            except Exception as e: _cache.update(date=day, at=time.time(), error=str(e)[:300])
        evs = [dict(e, ran_at=_ran.get(e["url"])) for e in _cache["events"]]
        return {"date": day, "fetched_at": _cache["at"], "events": evs, "error": _cache["error"], "cached": True}

def mark_ran(url):
    _ran[url] = datetime.now(TZ).strftime("%H:%M")
