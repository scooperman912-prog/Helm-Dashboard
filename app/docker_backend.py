"""Talks to the Docker Engine API (via the socket proxy in production).

A background sampler refreshes container data every SAMPLE_SECONDS so the API
answers instantly from a cache and keeps a 24 h CPU history per container
(used for the "24 h peak" in the detail pop-up).

CPU is reported two ways:
  cpu      — percent of the WHOLE server (all threads = 100 %), so container
             numbers add up to the host CPU figure.
  cpu_thr  — the same load in threads (1.0 = one thread fully busy).

If Docker can't be reached, everything falls back to demo data with demo=True.
"""
import os
import json
import time
import threading
import urllib.request
import concurrent.futures
from collections import deque

WUD_URL = os.environ.get("WUD_URL")  # e.g. http://host.docker.internal:3000
BACKUP_DIR = os.environ.get("BACKUP_DIR", "~/helm-backups")
SAMPLE_SECONDS = int(os.environ.get("SAMPLE_SECONDS", "10"))
HISTORY_POINTS = 24 * 3600 // max(SAMPLE_SECONDS, 10)

_lock = threading.Lock()
_cache = {"ts": 0, "demo": True, "error": None, "containers": []}
_history = {}  # name -> deque[(ts, cpu_host_pct)]
_sampler_started = False


# ---------------------------------------------------------------- demo data --
def _demo():
    now = time.time()

    def c(name, project, status, cpu_thr, mem, ver, image, port=None, update=None, started_h=72, restarts=0, health="healthy"):
        return {
            "id": name[:12], "name": name, "cname": name, "project": project, "exit_code": None, "status": status,
            "status_text": "Up" if status == "running" else status.capitalize(),
            "started_at": now - started_h * 3600, "restarts": restarts,
            "health": health if status == "running" else None,
            "image": image, "ver": ver, "cpu": round(cpu_thr * 100 / 8, 2), "cpu_thr": cpu_thr,
            "cpu_peak": round(cpu_thr * 100 / 8 * 2.4, 2), "mem_used": mem, "mem_limit": None,
            "ports": ([{"private": f"{port}/tcp", "public": port}] if port else []),
            "mounts": [{"src": f"/srv/{project or name}", "dst": "/config", "rw": True}],
            "networks": {"default": "172.18.0.2"}, "net_rx": int(mem * 4e6), "net_tx": int(mem * 2e6),
            "update": bool(update), "new_ver": update, "url": None, "labels": {},
        }
    return [
        c("jellyfin", None, "running", 0.75, 980, "10.11.3", "jellyfin/jellyfin:10.11.3", 8096, started_h=146),
        c("immich_server", "immich", "running", 0.38, 1450, "v1.135.3", "ghcr.io/immich-app/immich-server:v1.135.3", 2283, update="v1.136.0", started_h=273, restarts=1),
        c("immich_machine_learning", "immich", "running", 0.22, 2100, "v1.135.3", "ghcr.io/immich-app/immich-machine-learning:v1.135.3", started_h=273),
        c("immich_postgres", "immich", "running", 0.04, 380, "14", "tensorchord/pgvecto-rs:pg14", started_h=273),
        c("immich_redis", "immich", "running", 0.005, 12, "8", "valkey/valkey:8", started_h=273),
        c("nextcloud_app", "nextcloud", "running", 0.12, 610, "32.0.2", "nextcloud:32.0.2-apache", 8080, update="32.0.3", started_h=460, health=None),
        c("nextcloud_db", "nextcloud", "running", 0.03, 240, "11.4", "mariadb:11.4", started_h=460),
        c("nextcloud_redis", "nextcloud", "running", 0.004, 9, "7", "redis:7", started_h=460),
        c("qbittorrent", "downloads", "running", 0.14, 410, "5.1.2", "lscr.io/linuxserver/qbittorrent:5.1.2", 8081, started_h=361, restarts=2),
        c("sonarr", "downloads", "running", 0.015, 260, "4.0.15", "lscr.io/linuxserver/sonarr:4.0.15", 8989, started_h=361),
        c("radarr", "downloads", "running", 0.012, 240, "5.27.5", "lscr.io/linuxserver/radarr:5.27.5", 7878, started_h=361),
        c("prowlarr", "downloads", "running", 0.006, 150, "2.0.5", "lscr.io/linuxserver/prowlarr:2.0.5", 9696, started_h=361),
        c("adguardhome", None, "running", 0.009, 70, "v0.107.65", "adguard/adguardhome:v0.107.65", 3001, started_h=223, health=None),
        c("vaultwarden", None, "running", 0.001, 30, "1.34.3", "vaultwarden/server:1.34.3", 8222, started_h=984),
        c("game-server", None, "paused", 0, 0, "latest", "example/game-server:latest", started_h=500, health=None),
    ]


# ------------------------------------------------------------------ helpers --
def _client():
    import docker
    host = os.environ.get("DOCKER_HOST")
    if host:
        return docker.DockerClient(base_url=host, timeout=20)
    return docker.from_env()


def _cpu(s):
    """Return (percent of whole host, threads busy)."""
    try:
        cpu, pre = s["cpu_stats"], s["precpu_stats"]
        cd = cpu["cpu_usage"]["total_usage"] - pre["cpu_usage"]["total_usage"]
        sd = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu["cpu_usage"].get("percpu_usage") or []) or (os.cpu_count() or 1)
        if sd > 0 and cd > 0:
            host_pct = cd / sd * 100.0
            return round(host_pct, 2), round(host_pct * ncpu / 100.0, 3)
    except Exception:
        pass
    return 0.0, 0.0


def _mem(s):
    try:
        ms = s["memory_stats"]
        usage = ms["usage"]
        st = ms.get("stats") or {}
        cache = st.get("inactive_file", 0) or st.get("cache", 0)
        used = max(usage - cache, 0)
        limit = ms.get("limit") or 0
        return round(used / 1048576), round(limit / 1048576)
    except Exception:
        return 0, 0


def _net(s):
    rx = tx = 0
    for v in (s.get("networks") or {}).values():
        rx += v.get("rx_bytes", 0)
        tx += v.get("tx_bytes", 0)
    return rx, tx


def _norm_status(raw):
    if raw in ("running", "paused"):
        return raw
    if raw == "restarting":
        return "restarting"
    return "stopped"


def _ts(iso):
    """Docker ISO timestamp -> epoch seconds (None if unset)."""
    if not iso or iso.startswith("0001"):
        return None
    try:
        from datetime import datetime, timezone
        base = iso.split(".")[0].rstrip("Z")
        return datetime.strptime(base, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def _one(c, host_mem_mb):
    attrs = c.attrs or {}
    state = attrs.get("State") or {}
    cfg = attrs.get("Config") or {}
    ns = attrs.get("NetworkSettings") or {}
    labels = cfg.get("Labels") or {}
    raw = state.get("Status") or c.status
    s = {}
    if raw == "running":
        try:
            s = c.stats(stream=False)
        except Exception:
            s = {}
    cpu, thr = _cpu(s) if s else (0.0, 0.0)
    mem_used, mem_limit = _mem(s) if s else (0, 0)
    rx, tx = _net(s) if s else (0, 0)

    ver = labels.get("org.opencontainers.image.version")
    image = cfg.get("Image") or ""
    if not ver:
        ver = image.rsplit(":", 1)[-1] if ":" in image.split("/")[-1] else "latest"

    ports = []
    for priv, binds in (ns.get("Ports") or {}).items():
        pub = None
        for b in binds or []:
            if b.get("HostPort"):
                pub = int(b["HostPort"])
                break
        ports.append({"private": priv, "public": pub})
    ports.sort(key=lambda p: (p["public"] is None, p["public"] or 0))

    mounts = [{"src": m.get("Source") or m.get("Name"), "dst": m.get("Destination"), "rw": bool(m.get("RW"))}
              for m in (attrs.get("Mounts") or [])]
    networks = {k: (v or {}).get("IPAddress") for k, v in (ns.get("Networks") or {}).items()}
    health = (state.get("Health") or {}).get("Status")

    return {
        "id": c.id[:12],
        "name": labels.get("helm.name") or c.name,
        "cname": c.name,
        "project": labels.get("com.docker.compose.project") or None,
        "status": _norm_status(raw),
        "status_text": raw,
        "started_at": _ts(state.get("StartedAt")),
        "restarts": attrs.get("RestartCount", 0),
        "exit_code": state.get("ExitCode") if raw != "running" else None,
        "health": health,
        "image": image,
        "ver": ver,
        "cpu": cpu,
        "cpu_thr": thr,
        "mem_used": mem_used,
        # a limit equal to the host's RAM means "no limit set"
        "mem_limit": mem_limit if mem_limit and abs(mem_limit - host_mem_mb) > 64 else None,
        "ports": ports,
        "mounts": mounts,
        "networks": networks,
        "net_rx": rx,
        "net_tx": tx,
        "labels": {k: v for k, v in labels.items() if k.startswith("helm.")},
        "url": labels.get("helm.url"),
        "update": False,
        "new_ver": None,
    }


def _host_mem_mb():
    try:
        import psutil
        return psutil.virtual_memory().total // 1048576
    except Exception:
        return 0


# ------------------------------------------------------------------ sampler --
def sample():
    """Refresh the container cache (called by the background thread)."""
    try:
        cl = _client()
        conts = cl.containers.list(all=True)  # full inspect per container
        hm = _host_mem_mb()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            data = list(ex.map(lambda c: _one(c, hm), conts))
        ups = get_updates()
        for c in data:
            u = ups.get(c["cname"].lower())
            if u:
                c["update"] = bool(u.get("update"))
                c["new_ver"] = u.get("new")
        demo, err = False, None
    except Exception as e:
        data, demo, err = _demo(), True, str(e)

    now = time.time()
    for c in data:
        h = _history.setdefault(c["name"], deque(maxlen=HISTORY_POINTS))
        h.append((now, c["cpu"]))
        if not demo:
            c["cpu_peak"] = round(max(v for _, v in h), 2)
    data.sort(key=lambda x: x["name"].lower())
    with _lock:
        _cache.update({"ts": now, "demo": demo, "error": err, "containers": data})
    return _cache


def _loop():
    while True:
        try:
            sample()
        except Exception:
            pass
        time.sleep(SAMPLE_SECONDS)


def start_sampler():
    global _sampler_started
    if _sampler_started:
        return
    _sampler_started = True
    threading.Thread(target=_loop, name="helm-sampler", daemon=True).start()


def list_containers():
    with _lock:
        fresh = _cache["containers"] and time.time() - _cache["ts"] < SAMPLE_SECONDS * 3
        snap = dict(_cache)
    if not fresh:
        snap = dict(sample())
    return {"demo": snap["demo"], "error": snap["error"], "ts": snap["ts"],
            "threads": os.cpu_count() or 1, "containers": snap["containers"]}


def find(cid):
    for c in list_containers()["containers"]:
        if c["id"] == cid or c["name"] == cid or c.get("cname") == cid:
            return c
    return None


# ---------------------------------------------------------------------- WUD --
def _wud_get(path):
    if not WUD_URL:
        return None
    try:
        req = urllib.request.Request(WUD_URL.rstrip("/") + path, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=4) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _wud_post(path, payload):
    if not WUD_URL:
        return None
    try:
        body = json.dumps(payload or {}).encode()
        req = urllib.request.Request(WUD_URL.rstrip("/") + path, data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except Exception:
        return None


def get_updates():
    data = _wud_get("/api/containers")
    out = {}
    if not data:
        return out
    items = data if isinstance(data, list) else (data.get("data") or data.get("containers") or [])
    for it in items:
        if not isinstance(it, dict):
            continue
        name = (it.get("name") or "").lower()
        if not name:
            continue
        upd = it.get("updateAvailable")
        if upd is None:
            upd = bool(it.get("updateKind"))
        res = it.get("result") or {}
        out[name] = {"update": bool(upd), "new": res.get("tag")}
    return out


# ------------------------------------------------------------ safe updates --
def _dump_cmd(name, img, envnames):
    """DB-dump command that references in-container env vars (no secrets printed)."""
    img = img or ""
    out = " > " + BACKUP_DIR + "/" + name + "_$(date +%F).sql"
    if "mariadb" in img or "mysql" in img:
        pw = "MARIADB_ROOT_PASSWORD" if "MARIADB_ROOT_PASSWORD" in envnames else "MYSQL_ROOT_PASSWORD"
        return "docker exec " + name + " sh -c 'exec mysqldump --all-databases -uroot -p\"$" + pw + "\"'" + out
    if "postgres" in img or "postgres" in name or "pgvecto" in img:
        if "POSTGRES_USER" in envnames:
            return "docker exec -t " + name + " sh -c 'PGPASSWORD=$POSTGRES_PASSWORD pg_dumpall -U $POSTGRES_USER'" + out
        return "docker exec -t " + name + " pg_dumpall -U postgres" + out
    return None


def update_plan(cid):
    cl = _client()
    target = cl.containers.get(cid)
    labels = target.labels or {}
    project = labels.get("com.docker.compose.project")
    workdir = labels.get("com.docker.compose.project.working_dir")
    if project:
        members = [c for c in cl.containers.list(all=True)
                   if (c.labels or {}).get("com.docker.compose.project") == project]
    else:
        members = [target]
    dumps, current = [], []
    for c in members:
        cfg = c.attrs.get("Config", {}) or {}
        img = (cfg.get("Image") or "").lower()
        envnames = [e.split("=", 1)[0] for e in (cfg.get("Env") or [])]
        cmd = _dump_cmd(c.name, img, envnames)
        if cmd:
            dumps.append({"container": c.name, "cmd": cmd})
        current.append({"container": c.name, "image": cfg.get("Image")})
    update_cmd = (("cd " + workdir + " && ") if workdir else "") + "docker compose pull && docker compose up -d"
    return {"project": project or target.name, "workdir": workdir, "backup_dir": BACKUP_DIR,
            "dumps": dumps, "update_cmd": update_cmd, "current": current}


def update_container(cid):
    cl = _client()
    name = cl.containers.get(cid).name
    if WUD_URL:
        for path in (f"/api/containers/{name}/update", "/api/triggers/docker/update"):
            if _wud_post(path, {"container": name}) is not None:
                return {"ok": True, "via": "wud", "name": name}
    return {"ok": False, "name": name,
            "note": "Updater not reachable. Use the Safe Update plan, or enable WUD's docker trigger."}


# ------------------------------------------------------------------ actions --
_ACTS = {"start", "stop", "restart", "pause", "unpause"}


def action(cid, act):
    if act not in _ACTS:
        raise ValueError(f"unknown action: {act}")
    c = _client().containers.get(cid)
    getattr(c, act)()
    threading.Thread(target=sample, daemon=True).start()
    return {"ok": True, "action": act, "id": cid}


def project_action(project, act):
    if act not in _ACTS:
        raise ValueError(f"unknown action: {act}")
    cl = _client()
    members = [c for c in cl.containers.list(all=True)
               if (c.labels or {}).get("com.docker.compose.project") == project]
    if not members:
        raise ValueError("no containers in that stack")
    for c in members:
        try:
            getattr(c, act)()
        except Exception:
            pass
    threading.Thread(target=sample, daemon=True).start()
    return {"ok": True, "action": act, "project": project, "count": len(members)}


def logs(cid, tail=200):
    c = _client().containers.get(cid)
    raw = c.logs(tail=int(tail), timestamps=False)
    return raw.decode("utf-8", "replace")
