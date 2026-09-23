"""Rules-first triage. Same six autonomy dimensions and bands as local-ci gates.json, so a score here
means the same thing as `gate.py autonomy`. Every score is also appended to local-ci's receipts ledger."""
import json, os, re, uuid
from datetime import datetime
from .config import VIP, FIRE_DRILL_CHANNELS, LOCAL_CI_SCRIPT_DIRS, LOCAL_CI_HOME, TEAM, NOT_ASSIGNABLE

def _gates():
    for d in LOCAL_CI_SCRIPT_DIRS:
        p = os.path.join(d, "gates.json")
        if os.path.exists(p):
            try: return json.load(open(p))["autonomy"]
            except Exception: pass
    return {"bands": [[6, 9, "PASS", "build alone"], [10, 13, "WARN", "post intent, then proceed"], [14, 18, "BLOCK", "align first"]],
            "dimensions": [{"id": k} for k in ("lift", "time", "scope", "impact", "risk", "novelty")]}

def band_for(total):
    for lo, hi, name, _ in _gates()["bands"]:
        if lo <= total <= hi: return name
    return "BLOCK" if total > 13 else "PASS"

def vip_tier(name):
    n = (name or "").lower()
    for tier, names in VIP.items():
        if any(x in n for x in names): return tier
    return "ic"

URGENT_WORDS = re.compile(r"\b(today|asap|urgent|now|blocked|broken|not working|down|can'?t|aren'?t|isn'?t|fire drill|stuck|still)\b", re.I)
IMPORTANT_WORDS = re.compile(r"\b(moneyball|fastball|pitchr|pulse|assign|unassign|icp|tam|lead|demo|rep|revenue|romwell|setter)\b", re.I)
BUILD_WORDS = re.compile(r"\b(build|add|feature|prd|can we|could we|would be nice|automate|dashboard|report)\b", re.I)

def score(text, requester=None, channel=None, replies=0):
    """Returns dict(urgent, important, quadrant, route, scores, total, band, rationale)."""
    t = text or ""; tier = vip_tier(requester); ch = (channel or "").lstrip("#")
    urgent = bool(URGENT_WORDS.search(t)) or tier in ("csuite", "vp") or (ch in FIRE_DRILL_CHANNELS and replies >= 3)
    important = bool(IMPORTANT_WORDS.search(t)) or tier in ("csuite", "vp", "director")
    quadrant = "Q1" if urgent and important else "Q2" if important else "Q3" if urgent else "Q4"
    is_build = bool(BUILD_WORDS.search(t))
    # six dims, 1..3 each, defaults mid; rules nudge
    s = {"lift": 2 if is_build else 1, "time": 1 if urgent else 3 if tier == "ic" else 2,
         "scope": 2 if is_build else 1, "impact": 3 if tier in ("csuite", "vp", "director") or ch in FIRE_DRILL_CHANNELS else 2,
         "risk": 2 if IMPORTANT_WORDS.search(t) else 1, "novelty": 2 if is_build else 1}
    total = sum(s.values()); b = band_for(total)
    route = "team" if is_build and b != "BLOCK" else "prd" if is_build else "me" if quadrant in ("Q1", "Q2") else "park" if quadrant == "Q3" else "noise"
    why = []
    if tier != "ic": why.append(f"requester tier {tier}")
    if ch in FIRE_DRILL_CHANNELS: why.append(f"#{ch} is a fire-drill channel")
    if is_build: why.append("reads as a build request")
    if URGENT_WORDS.search(t): why.append("urgency language")
    return {"urgent": int(urgent), "important": int(important), "quadrant": quadrant, "route": route,
            "scores": s, "total": total, "band": b, "vip_tier": tier,
            "rationale": "; ".join(why) or "no strong signals; defaulted to Q2/me"}

def receipt(title, scores, band, task_id):
    """Append to the same ledger gate.py writes, so intake decisions show up in local-ci history."""
    try:
        os.makedirs(LOCAL_CI_HOME, exist_ok=True)
        row = {"id": datetime.now().strftime("%Y%m%d-") + uuid.uuid4().hex[:6], "gate": "autonomy",
               "verdict": band, "at": datetime.now().isoformat(timespec="seconds"), "cwd": "command-center",
               "answers": {**{k: str(v) for k, v in scores.items()}, "item": title[:160], "task_id": task_id},
               "source": "command-center/intake"}
        with open(os.path.join(LOCAL_CI_HOME, "receipts.jsonl"), "a") as f: f.write(json.dumps(row) + "\n")
    except Exception: pass

def suggest_assignee(loads):
    """loads: login -> numeric load (server computes). Least-loaded engineer, or None."""
    cands = [(l, loads.get(l, 0)) for l in TEAM if l not in NOT_ASSIGNABLE]
    return min(cands, key=lambda x: x[1])[0] if cands else None
