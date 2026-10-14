"""Service links + "last accessed", read from Nginx Proxy Manager (optional).

NPM_DATA_DIR points at NPM's /data folder mounted read-only, e.g.
    - /path/to/npm/data:/npm:ro      and   NPM_DATA_DIR=/npm
From it we read each proxy host's config (domain -> forward host:port) and
the last line of its access log. A proxy host is matched to a container when
its forward host is the container's name / IP, or its forward port is one the
container publishes.

Link priority for each container:
  1. label  helm.url=https://...
  2. env    SERVICE_URLS="jellyfin=https://...,immich=https://..."
            (key = container name or compose project)
  3. its NPM proxy host  -> https://<domain>
  4. none   (the UI falls back to http://<this host>:<published port>)
"""
import os
import re
import glob
import time
from datetime import datetime

import settings


def _npm_dir():
    return (settings.get("npm_data_dir") or "").rstrip("/")


_TTL = 30
_cache = {"ts": 0, "hosts": []}

_TS = re.compile(r"\[(\d{2}/\w{3}/\d{4}:\d{2}:\d{2}:\d{2} [+-]\d{4})\]")
_UA = re.compile(r'"([^"]*)"\s+"[^"]*"\s*$')


def _service_urls():
    out = {}
    for part in (settings.get("service_urls") or "").split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip().lower()] = v.strip()
    return out


def _tail(path, size=4096):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            f.seek(max(0, end - size))
            lines = f.read().decode("utf-8", "replace").strip().splitlines()
            return lines[-1] if lines else ""
    except Exception:
        return ""


def _client_label(ua):
    u = (ua or "").lower()
    for key, label in (("roku", "Roku"), ("webos", "LG TV"), ("tizen", "Samsung TV"), ("appletv", "Apple TV"),
                       ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"), ("macintosh", "Mac"),
                       ("windows", "Windows"), ("nextcloud", "Nextcloud client"), ("immich", "Immich app"),
                       ("bitwarden", "Bitwarden"), ("jellyfin", "Jellyfin app"), ("curl", "script")):
        if key in u:
            return label
    return "browser" if "mozilla" in u else None


def _proxy_hosts():
    npm = _npm_dir()
    if not npm or not os.path.isdir(npm):
        return []
    if time.time() - _cache["ts"] < _TTL:
        return _cache["hosts"]
    hosts = []
    for conf in glob.glob(os.path.join(npm, "nginx/proxy_host/*.conf")):
        hid = os.path.splitext(os.path.basename(conf))[0]
        try:
            txt = open(conf).read()
        except Exception:
            continue
        server = re.search(r'set \$server\s+"?([^";]+)"?;', txt)
        port = re.search(r"set \$port\s+(\d+);", txt)
        names = re.search(r"server_name\s+([^;]+);", txt)
        if not server or not names:
            continue
        domains = names.group(1).split()
        last = _tail(os.path.join(npm, f"logs/proxy-host-{hid}_access.log"))
        ts = None
        m = _TS.search(last)
        if m:
            try:
                ts = datetime.strptime(m.group(1), "%d/%b/%Y:%H:%M:%S %z").timestamp()
            except Exception:
                ts = None
        ua = _UA.search(last)
        hosts.append({"server": server.group(1).strip().lower(), "port": int(port.group(1)) if port else None,
                      "domain": domains[0], "last": ts, "client": _client_label(ua.group(1) if ua else "")})
    _cache.update({"ts": time.time(), "hosts": hosts})
    return hosts


def _match(c, hosts):
    names = {c.get("cname", "").lower(), c.get("name", "").lower()}
    ips = {ip for ip in (c.get("networks") or {}).values() if ip}
    pubs = {p["public"] for p in c.get("ports") or [] if p.get("public")}
    privs = set()
    for p in c.get("ports") or []:
        try:
            privs.add(int(p["private"].split("/")[0]))
        except Exception:
            pass
    for h in hosts:
        if h["server"] in names or h["server"] in ips:
            if h["port"] is None or h["port"] in pubs | privs:
                return h
    for h in hosts:  # forwarded to the host's IP / host.docker.internal on a published port
        if h["port"] and h["port"] in pubs:
            return h
    return None


def enrich(containers):
    """Add url / domain / last_access / last_client to each container dict (in place)."""
    hosts = _proxy_hosts()
    urls = _service_urls()
    for c in containers:
        h = _match(c, hosts) if hosts else None
        url = (c.get("labels") or {}).get("helm.url") or urls.get(c.get("cname", "").lower()) \
            or urls.get(c.get("name", "").lower()) or urls.get((c.get("project") or "").lower())
        if not url and h:
            url = "https://" + h["domain"]
        c["url"] = url
        c["domain"] = h["domain"] if h else None
        c["last_access"] = h["last"] if h else None
        c["last_client"] = h["client"] if h else None
    return containers


def tracking_enabled():
    return bool(_npm_dir() and os.path.isdir(_npm_dir()))
