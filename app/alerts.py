"""The "Needs attention" feed: computed alerts + per-alert silencing + phone pushes.

Alerts are derived on the fly from live data (drives, updates, crashed /
unhealthy / paused containers, backups). What the user did to them is stored in
JARVIS_DATA/alerts.json:

  dismissed  {id: rank}        hidden until the problem gets worse (rank goes up)
  snoozed    {id: {...}}       {"until": ts} | {"fp": fingerprint} (until it changes) | {"never": True}
  muted      bool              stop phone pushes (alerts still show in the UI)
  notified   {id: fingerprint} what we've already pushed, so each alert pushes once

Phone pushes use ntfy if NTFY_URL is set (e.g. https://ntfy.sh/<your-secret-topic>).
A background thread checks every ALERT_INTERVAL seconds.
"""
import os
import json
import time
import threading
import urllib.request

_PATH = os.path.join(os.environ.get("JARVIS_DATA", "/data"), "alerts.json")
NTFY_URL = os.environ.get("NTFY_URL", "").strip()
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()
INTERVAL = int(os.environ.get("ALERT_INTERVAL", "300"))
BACKUP_MAX_AGE_H = float(os.environ.get("BACKUP_MAX_AGE_HOURS", "192"))

_lock = threading.Lock()
_sources = {}


def configure(containers, drives, backups):
    """Callables returning the current data (wired up by main.py)."""
    _sources.update(containers=containers, drives=drives, backups=backups)


# -------------------------------------------------------------------- state --
def _load():
    try:
        with open(_PATH) as f:
            s = json.load(f)
    except Exception:
        s = {}
    for k, d in (("dismissed", {}), ("snoozed", {}), ("notified", {}), ("muted", False)):
        s.setdefault(k, d)
    return s


def _save(s):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f)
        os.replace(tmp, _PATH)
    except Exception:
        pass


# ------------------------------------------------------------------ compute --
def _fmt_gb(gb):
    return f"{gb / 1000:.2f} TB" if gb >= 1000 else f"{round(gb)} GB"


def compute():
    out = []
    try:
        drives = _sources["drives"]() or []
    except Exception:
        drives = []
    for d in drives:
        pct = d.get("pct", 0)
        if pct >= 80:
            rank = 3 if pct >= 95 else 2 if pct >= 90 else 1
            out.append({"id": "drive:" + d["name"], "level": "crit" if pct >= 92 else "warn", "rank": rank,
                        "fp": f"{rank}", "title": f"{d['name']} is {pct}% full",
                        "detail": f"~{_fmt_gb(max(d['total'] - d['used'], 0))} left on {d.get('mount', '')}",
                        "action": {"type": "storage", "label": "Open"}})
    try:
        cs = _sources["containers"]() or []
    except Exception:
        cs = []
    ups = [c for c in cs if c.get("update")]
    if ups:
        names = ", ".join(f"{c['name']} {c.get('new_ver') or ''}".strip() for c in ups[:4])
        more = f" +{len(ups) - 4} more" if len(ups) > 4 else ""
        out.append({"id": "updates", "level": "info", "rank": len(ups),
                    "fp": "|".join(sorted(f"{c['name']}:{c.get('new_ver')}" for c in ups)),
                    "title": f"{len(ups)} update{'s' if len(ups) > 1 else ''} available",
                    "detail": names + more, "action": {"type": "update", "label": "Review", "id": ups[0]["id"]}})
    for c in cs:
        if c.get("health") == "unhealthy":
            out.append({"id": "unhealthy:" + c["cname"], "level": "crit", "rank": 2, "fp": "unhealthy",
                        "title": f"{c['name']} is unhealthy", "detail": "Its health check is failing",
                        "action": {"type": "restart", "label": "Restart", "id": c["id"]}})
        elif c.get("status") == "restarting" or (c.get("status") == "stopped" and c.get("exit_code") not in (0, None)):
            out.append({"id": "down:" + c["cname"], "level": "crit", "rank": 2, "fp": str(c.get("exit_code")),
                        "title": f"{c['name']} has stopped", "detail": f"Exited with code {c.get('exit_code')}",
                        "action": {"type": "start", "label": "Start", "id": c["id"]}})
        elif c.get("status") == "paused":
            out.append({"id": "paused:" + c["cname"], "level": "muted", "rank": 1, "fp": "paused",
                        "title": f"{c['name']} is paused", "detail": "Paused containers keep their memory reserved",
                        "action": {"type": "unpause", "label": "Resume", "id": c["id"]}})
    try:
        b = _sources["backups"]() or {}
    except Exception:
        b = {}
    if b.get("ok"):
        failed = [k for k, v in (b.get("databases") or {}).items() if v.get("status") != "ok"]
        if b.get("snapshot") not in (None, "ok") or failed:
            out.append({"id": "backup:failed", "level": "crit", "rank": 2, "fp": str(b.get("finished")),
                        "title": "Last backup had errors",
                        "detail": "Failed: " + (", ".join(failed) if failed else "snapshot"),
                        "action": {"type": "backup", "label": "Back up now"}})
        fin = b.get("finished")
        try:
            from datetime import datetime
            age_h = (time.time() - datetime.fromisoformat(str(fin).replace("Z", "+00:00")).timestamp()) / 3600
        except Exception:
            age_h = None
        if age_h is not None and age_h > BACKUP_MAX_AGE_H:
            out.append({"id": "backup:stale", "level": "warn", "rank": 1, "fp": str(fin),
                        "title": "Backups are overdue", "detail": f"Last one ran {round(age_h / 24)} days ago",
                        "action": {"type": "backup", "label": "Back up now"}})
    return out


def _silence(a, s, now):
    """Return a human description if this alert is silenced, else None."""
    d = s["dismissed"].get(a["id"])
    if d is not None and a["rank"] <= d:
        return "dismissed — comes back if it gets worse"
    z = s["snoozed"].get(a["id"])
    if z:
        if z.get("never"):
            return "never alert on this"
        if z.get("until") and z["until"] > now:
            mins = int((z["until"] - now) / 60)
            return f"silenced for {mins // 60} h {mins % 60} min more" if mins >= 60 else f"silenced for {mins} min more"
        if z.get("fp") is not None and z["fp"] == a["fp"]:
            return "silenced until it changes"
    return None


def view():
    now = time.time()
    with _lock:
        s = _load()
    active, silenced = [], []
    alerts = compute()
    ids = {a["id"] for a in alerts}
    for a in alerts:
        how = _silence(a, s, now)
        (silenced if how else active).append(dict(a, how=how) if how else a)
    for aid, z in s["snoozed"].items():  # "never" rules whose alert isn't active right now
        if z.get("never") and aid not in ids:
            silenced.append({"id": aid, "title": z.get("title") or aid, "how": "never alert on this"})
    order = {"crit": 0, "warn": 1, "info": 2, "muted": 3}
    active.sort(key=lambda a: order.get(a["level"], 9))
    return {"alerts": active, "silenced": silenced, "muted": s["muted"], "push": bool(NTFY_URL)}


# ------------------------------------------------------------------ actions --
def _mutate(fn):
    with _lock:
        s = _load()
        fn(s)
        _save(s)


def _current(aid):
    return next((a for a in compute() if a["id"] == aid), None)


def dismiss(aid):
    a = _current(aid)
    _mutate(lambda s: s["dismissed"].__setitem__(aid, a["rank"] if a else 0))


def clear_all():
    alerts = view()["alerts"]

    def f(s):
        for a in alerts:
            s["dismissed"][a["id"]] = a["rank"]
    _mutate(f)


def snooze(aid, mode):
    a = _current(aid)
    if mode == "1h":
        z = {"until": time.time() + 3600}
    elif mode == "1d":
        z = {"until": time.time() + 86400}
    elif mode == "change":
        z = {"fp": a["fp"] if a else None}
    elif mode == "never":
        z = {"never": True, "title": a["title"] if a else aid}
    else:
        raise ValueError("mode must be 1h, 1d, change or never")
    _mutate(lambda s: s["snoozed"].__setitem__(aid, z))


def restore(aid):
    def f(s):
        s["dismissed"].pop(aid, None)
        s["snoozed"].pop(aid, None)
    _mutate(f)


def set_muted(v):
    _mutate(lambda s: s.__setitem__("muted", bool(v)))


# ----------------------------------------------------------------- notifier --
def _push(a):
    body = a["detail"].encode()
    req = urllib.request.Request(NTFY_URL, data=body, method="POST")
    req.add_header("Title", ("Helm: " + a["title"]).encode("ascii", "replace").decode())
    req.add_header("Priority", "high" if a["level"] == "crit" else "default")
    req.add_header("Tags", {"crit": "rotating_light", "warn": "warning"}.get(a["level"], "information_source"))
    if NTFY_TOKEN:
        req.add_header("Authorization", "Bearer " + NTFY_TOKEN)
    urllib.request.urlopen(req, timeout=8).read()


def check_and_notify():
    v = view()
    with _lock:
        s = _load()
        active_ids = {a["id"] for a in v["alerts"]} | {a["id"] for a in v["silenced"]}
        for aid in list(s["notified"]):
            if aid not in active_ids:
                s["notified"].pop(aid)  # resolved — notify again if it comes back
        to_push = [] if (s["muted"] or not NTFY_URL) else \
            [a for a in v["alerts"] if s["notified"].get(a["id"]) != a["fp"]]
        for a in v["alerts"]:
            s["notified"][a["id"]] = a["fp"]
        _save(s)
    for a in to_push:
        try:
            _push(a)
        except Exception:
            pass


def _loop():
    time.sleep(30)  # let the container sampler warm up first
    while True:
        try:
            check_and_notify()
        except Exception:
            pass
        time.sleep(INTERVAL)


_started = False


def start_notifier():
    global _started
    if not _started:
        _started = True
        threading.Thread(target=_loop, name="helm-alerts", daemon=True).start()
