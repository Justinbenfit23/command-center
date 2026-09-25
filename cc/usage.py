"""Claude plan usage (the same numbers as the Usage panel in the Claude app / `/usage` in Claude Code).

Reads the Claude Code OAuth token from the macOS keychain and calls the oauth usage endpoint.
Nothing is stored; the token never leaves this process except in the Authorization header to Anthropic."""
import json, subprocess, time, urllib.request, datetime

URL = "https://api.anthropic.com/api/oauth/usage"
_cache = {"at": 0, "data": None}
TTL = 60

def _token():
    out = subprocess.run(["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
                         capture_output=True, text=True, timeout=10).stdout.strip()
    if not out: raise RuntimeError("no Claude Code credentials in the keychain; run `claude` and log in")
    return json.loads(out).get("claudeAiOauth", {}).get("accessToken") or ""

def _fmt_reset(iso):
    if not iso: return None
    try:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        now = datetime.datetime.now().astimezone()
        same_day = dt.date() == now.date()
        label = dt.strftime("%-I:%M %p") if same_day else dt.strftime("%a %-I:%M %p")
        mins = int((dt - now).total_seconds() // 60)
        left = f"{mins // 60}h {mins % 60}m" if mins >= 60 else f"{mins}m"
        return {"iso": iso, "label": label, "in": left}
    except Exception:
        return {"iso": iso, "label": iso, "in": None}

def fetch(refresh=False):
    if not refresh and _cache["data"] and time.time() - _cache["at"] < TTL: return _cache["data"]
    tok = _token()
    if not tok: raise RuntimeError("Claude Code token missing; run `claude` and log in")
    req = urllib.request.Request(URL, headers={"Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20",
                                               "Content-Type": "application/json", "User-Agent": "command-center/2"})
    with urllib.request.urlopen(req, timeout=20) as r: raw = json.loads(r.read().decode())
    limits = []
    for l in raw.get("limits") or []:
        scope = l.get("scope") or {}
        model = ((scope.get("model") or {}).get("display_name")) if scope else None
        kind = l.get("kind")
        name = {"session": "Current session", "weekly_all": "This week"}.get(kind) or (f"{model} this week" if model else kind)
        limits.append({"kind": kind, "name": name, "percent": l.get("percent"), "severity": l.get("severity"),
                       "resets": _fmt_reset(l.get("resets_at")), "scope_model": model, "is_active": l.get("is_active")})
    extra = raw.get("extra_usage") or {}
    spend = raw.get("spend") or {}
    data = {"fetched_at": datetime.datetime.now().astimezone().strftime("%-I:%M:%S %p"), "limits": limits,
            "credits": {"enabled": extra.get("is_enabled"), "used": extra.get("used_credits"), "limit": extra.get("monthly_limit"),
                        "utilization": extra.get("utilization"), "spend_percent": spend.get("percent")},
            "breakdown": raw.get("seven_day_breakdown")}
    _cache.update(at=time.time(), data=data)
    return data
