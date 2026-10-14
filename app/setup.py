"""First-run setup + Settings: auto-detection and connection tests.

Helm sees the host's filesystem read-only at HOST_ROOT and the container list
through the socket proxy, so most things can be found without the user typing
paths: mounted drives, likely media folders, and well-known companion services
(What's Up Docker, AdGuard Home, Nginx Proxy Manager, Tailscale).
"""
import os
import re
import json
import secrets
import urllib.request

import settings
import docker_backend as db
import hostinfo

ROOT = settings.HOST_ROOT
_SKIP_FS = {"tmpfs", "devtmpfs", "overlay", "squashfs", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
            "mqueue", "debugfs", "tracefs", "securityfs", "pstore", "bpf", "configfs", "fusectl",
            "autofs", "binfmt_misc", "hugetlbfs", "nsfs", "efivarfs", "ramfs", "rpc_pipefs", "nfsd"}
_SKIP_MNT = re.compile(r"^/(boot|snap|var/lib/docker|var/snap|run|proc|sys|dev)(/|$)")


def _host(p):
    return settings.show_path(p)


def _label(mount):
    if mount == "/":
        return "System"
    base = os.path.basename(mount.rstrip("/"))
    return re.sub(r"[-_]+", " ", base).strip().title() or mount


# ------------------------------------------------------------------ drives --
def detect_drives():
    try:
        import psutil
    except Exception:
        return []
    seen, out = set(), []
    parts = psutil.disk_partitions(all=True)
    for p in parts:
        mp = p.mountpoint
        if not (mp == ROOT or mp.startswith(ROOT + "/")):
            continue
        host = _host(mp)
        if p.fstype in _SKIP_FS or _SKIP_MNT.match(host) or host.startswith("/host/"):
            continue
        try:
            u = psutil.disk_usage(mp)
        except Exception:
            continue
        if u.total < 1e9:  # under 1 GB: not a data drive
            continue
        key = (p.device, u.total)
        if key in seen:
            continue
        seen.add(key)
        out.append({"name": _label(host), "path": mp, "shown": host, "device": p.device, "fstype": p.fstype,
                    "used": round(u.used / 1e9), "total": round(u.total / 1e9), "pct": round(u.percent)})
    out.sort(key=lambda d: (d["shown"] != "/", d["shown"]))
    return out


# ----------------------------------------------------------------- folders --
def detect_folders():
    cands = []
    for base in ("srv", "media", "mnt", "home", "data", "opt/media", "storage"):
        b = os.path.join(ROOT, base)
        if not os.path.isdir(b):
            continue
        cands.append(b)
        try:
            for name in sorted(os.listdir(b))[:30]:
                full = os.path.join(b, name)
                if os.path.isdir(full) and not name.startswith(".") and name not in ("lost+found",):
                    cands.append(full)
        except Exception:
            pass
    for d in detect_drives():
        if d["shown"] != "/" and d["path"] not in cands:
            cands.append(d["path"])
    out, seen = [], set()
    for c in cands:
        rp = os.path.realpath(c)
        if rp in seen:
            continue
        seen.add(rp)
        out.append({"name": _label(_host(c)), "path": c, "shown": _host(c), "writable": os.access(c, os.W_OK)})
    return out


def write_snippet(paths):
    """Compose override that makes these host folders writable for the file manager."""
    lines = ["services:", "  helm:", "    volumes:"]
    for p in paths:
        hp = _host(p)
        lines.append(f"      - {hp}:{ROOT.rstrip('/')}{hp}:rw")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- services --
def _port_for(c, private_ports):
    for p in c.get("ports") or []:
        try:
            priv = int(p["private"].split("/")[0])
        except Exception:
            continue
        if priv in private_ports and p.get("public"):
            return p["public"]
    for p in c.get("ports") or []:
        if p.get("public") and "/tcp" in p.get("private", ""):
            return p["public"]
    return None


def detect_services():
    cs = db.list_containers()
    found = {"docker": not cs["demo"]}
    if cs["demo"]:
        return found
    for c in cs["containers"]:
        img = (c.get("image") or "").lower()
        if any(k in img for k in ("whats-up-docker", "fmartinou/wud", "getwud/wud", "/wud:")) and "wud" not in found:
            port = _port_for(c, {3000})
            found["wud"] = {"container": c["cname"], "url": f"http://host.docker.internal:{port or 3000}"}
        elif "adguardhome" in img and "adguard" not in found:
            port = _port_for(c, {80, 3000})
            found["adguard"] = {"container": c["cname"], "url": f"http://host.docker.internal:{port or 80}",
                                "host_network": not c.get("ports")}
        elif "nginx-proxy-manager" in img and "npm" not in found:
            src = next((m["src"] for m in c.get("mounts") or [] if m.get("dst") == "/data"), None)
            path = os.path.join(ROOT, src.lstrip("/")) if src else None
            found["npm"] = {"container": c["cname"], "path": path if path and os.path.isdir(path) else None,
                            "shown": src}
    sock = hostinfo.ts_socket()
    if sock:
        found["tailscale"] = {"socket": sock}
    return found


def detect_backup_status():
    hits = []
    home = os.path.join(ROOT, "home")
    try:
        for u in os.listdir(home):
            for d in ("helm-backups", "jarvis-backups", "backups"):
                p = os.path.join(home, u, d, "status.json")
                if os.path.isfile(p):
                    hits.append({"path": p, "shown": _host(p),
                                 "command": f"~/{d}/run-backup.sh" if os.path.exists(os.path.join(home, u, d, "run-backup.sh")) else ""})
    except Exception:
        pass
    return hits


def detect():
    return {"drives": detect_drives(), "folders": detect_folders(), "services": detect_services(),
            "backups": detect_backup_status(), "host_root": ROOT}


# ------------------------------------------------------------------- tests --
def test_wud(url):
    if not (url or "").strip():
        return {"ok": False, "message": "Enter an address first"}
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/api/containers", timeout=5) as r:
            data = json.loads(r.read().decode())
        n = len(data) if isinstance(data, list) else len(data.get("data") or [])
        return {"ok": True, "message": f"Connected — watching {n} containers"}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't reach What's Up Docker: {e}"}


def test_adguard(url, user, pw):
    if not (url or "").strip():
        return {"ok": False, "message": "Enter an address first"}
    if pw is None:
        pw = settings.get("adguard_password")
    r = hostinfo.adguard(url, user, pw)
    if r.get("error"):
        msg = r["error"]
        if "401" in msg or "403" in msg:
            msg = "Wrong username or password"
        return {"ok": False, "message": msg}
    return {"ok": True, "message": f"Connected — {r['queries']:,} queries, {r['blocked_pct']}% blocked"}


def test_npm(path):
    if not path or not os.path.isdir(path):
        return {"ok": False, "message": "Folder not found"}
    n = len([f for f in os.listdir(os.path.join(path, "nginx/proxy_host")) if f.endswith(".conf")]) \
        if os.path.isdir(os.path.join(path, "nginx/proxy_host")) else 0
    if not n:
        return {"ok": False, "message": "No proxy hosts found there — is this NPM's /data folder?"}
    return {"ok": True, "message": f"Found {n} proxy host{'s' if n != 1 else ''}"}


def test_ntfy(url, token):
    if not (url or "").strip():
        return {"ok": False, "message": "Generate or enter a topic first"}
    import alerts
    try:
        alerts._push({"title": "Test notification", "detail": "If you can read this, Helm can reach your phone.",
                      "level": "info"}, url=url, token=token)
        return {"ok": True, "message": "Sent — check your phone"}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't send: {e}"}


def test_assistant(url, model, key):
    if not (url or "").strip() or not (model or "").strip():
        return {"ok": False, "message": "Enter the server address and model name"}
    import assistant
    try:
        reply = assistant._call_model([{"role": "user", "content": "Reply with just: ready"}], {}, url, model, key)
        return {"ok": True, "message": f"Model replied: {reply[:60]}"}
    except Exception as e:
        return {"ok": False, "message": f"Couldn't reach the model: {e}"}


def new_ntfy_topic():
    return "https://ntfy.sh/helm-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:22].lower()
