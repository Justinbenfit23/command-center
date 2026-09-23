"""Big rocks -> Shipyard tasks. The mapping is generated once per task by `claude -p` and cached in rock_map;
The operator can overwrite any row from the UI. Mermaid text is built from the cache so the DAG is static and cheap."""
import json, subprocess
from .config import ROCKS, TEAM, ME, ME_UID, CLAUDE_CWD
from . import db, shipyard

ENGINEERS = {t[1]: (login, t[0]) for login, t in TEAM.items()}
ENGINEERS[ME_UID] = (ME, "Me")
ENG_COLORS = ["#9ece6a", "#e0af68", "#bb9af7", "#7dcfff", "#f7768e", "#7aa2f7"]
_ENG_SLOTS = {}

def _slot(name):
    """Stable colour index per engineer, assigned in first-seen order."""
    return _ENG_SLOTS.setdefault(name, len(_ENG_SLOTS) % len(ENG_COLORS))

def open_team_tasks():
    seen, out = set(), []
    for uid in list(ENGINEERS):
        for t in shipyard.open_tasks(assignee_uid=uid):
            if t["key"] in seen: continue
            seen.add(t["key"]); t["assignee_uid"] = uid; t["engineer"] = ENGINEERS[uid][1]; out.append(shipyard.enrich(t))
    return out

def classify(tasks):
    """One claude -p call for every unmapped task. Returns {key: {rock_id, rationale}}."""
    todo = [t for t in tasks if t["key"] not in db.rock_map()]
    if not todo: return {}
    rocks = "\n".join(f"- {r['id']}: {r['title']} — {r['blurb']}" for r in ROCKS)
    lines = "\n".join(f"- {t['key']}: {t['title']}" for t in todo)
    prompt = (f"Map each Shipyard task to exactly one big rock. Rocks:\n{rocks}\n\nTasks:\n{lines}\n\n"
              "Reply ONLY with a JSON object: {\"GTMP-123\": {\"rock_id\": \"...\", \"rationale\": \"one plain sentence on how it moves that rock\"}, ...}. "
              "If a task moves none of them, use rock_id \"none\" and say why.")
    r = subprocess.run(["claude", "-p", prompt, "--output-format", "json"], capture_output=True, text=True, timeout=300, cwd=CLAUDE_CWD)
    try:
        txt = json.loads(r.stdout).get("result", ""); s, e = txt.find("{"), txt.rfind("}"); m = json.loads(txt[s:e + 1])
    except Exception:
        return {}
    for k, v in m.items():
        if isinstance(v, dict): db.set_rock(k, v.get("rock_id", "none"), v.get("rationale", ""))
    return m

def view(tasks=None, auto_map=False):
    tasks = tasks if tasks is not None else open_team_tasks()
    if auto_map: classify(tasks)
    rm = db.rock_map()
    rocks = [{**r, "tasks": []} for r in ROCKS] + [{"id": "none", "title": "Not on a rock", "blurb": "Keeps the lights on, or unmapped.", "tasks": []}]
    by = {r["id"]: r for r in rocks}
    for t in tasks:
        m = rm.get(t["key"]) or {"rock_id": "none", "rationale": ""}
        by.get(m["rock_id"], by["none"])["tasks"].append({**t, "rationale": m.get("rationale", "")})
    by_eng = {}
    for t in tasks: by_eng.setdefault(t["engineer"], []).append({**t, "rock_id": (rm.get(t["key"]) or {}).get("rock_id", "none")})
    return {"rocks": rocks, "by_engineer": by_eng, "mermaid": mermaid(rocks)}

def mermaid(rocks):
    q = lambda s: s.replace('"', "'")[:48]
    lines = ["graph LR"]
    for r in rocks:
        if r["id"] == "none" and not r["tasks"]: continue
        lines.append(f'  {r["id"]}["{q(r["title"])}"]:::rock')
        for t in r["tasks"]:
            n = t["key"].replace("-", "_")
            lines.append(f'  {n}["{t["key"]} · {q(t["title"])}<br/><i>{q(t["engineer"])}</i>"]:::eng{_slot(t["engineer"])}')
            lines.append(f"  {r['id']} --> {n}")
            lines.append(f'  click {n} "{t["url"]}" _blank')
    lines.append("  classDef rock fill:#1f2b3b,stroke:#7aa2f7,color:#e6e8ef,font-weight:bold")
    for i, c in enumerate(ENG_COLORS): lines.append(f"  classDef eng{i} fill:#22242c,stroke:{c},color:#e6e8ef")
    return "\n".join(lines)
