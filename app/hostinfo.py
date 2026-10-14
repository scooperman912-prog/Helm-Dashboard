"""Host-level readers: CPU (total + per thread), memory, network throughput,
Tailscale and AdGuard Home. Every reader is optional and fails soft — a panel
that isn't configured simply reports {"enabled": False}.

HOST_ROOT is where the host's / is mounted read-only (default /host/root).
Host network counters come from HOST_ROOT/sys/class/net, which shows the
host's interfaces even though the dashboard runs inside a container.
"""
import os
import re
import json
import time
import base64
import socket
import http.client
import threading
import urllib.request

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

import settings

HOST_ROOT = settings.HOST_ROOT


def ts_socket():
    """Configured tailscaled socket, else the first standard location that exists
    (the host's /run is visible through the read-only HOST_ROOT mount)."""
    p = settings.get("tailscale_socket")
    if p:
        return p
    for c in (os.path.join(HOST_ROOT, "run/tailscale/tailscaled.sock"),
              os.path.join(HOST_ROOT, "var/run/tailscale/tailscaled.sock"),
              "/var/run/tailscale/tailscaled.sock"):
        if os.path.exists(c):
            return c
    return None


# ------------------------------------------------------------------- system --
_cpu_model = None


def _cpuinfo_model():
    global _cpu_model
    if _cpu_model is None:
        _cpu_model = ""
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.lower().startswith("model name"):
                        _cpu_model = re.sub(r"\s+", " ", line.split(":", 1)[1]).strip()
                        _cpu_model = re.sub(r" with Radeon.*$| \d+-Core Processor$|\(R\)|\(TM\)", "", _cpu_model).strip()
                        break
        except Exception:
            pass
    return _cpu_model


def _hostname():
    for p in (os.path.join(HOST_ROOT, "etc/hostname"), "/etc/hostname"):
        try:
            with open(p) as f:
                h = f.read().strip()
                if h:
                    return h
        except Exception:
            continue
    return socket.gethostname()


def _os_name():
    try:
        with open(os.path.join(HOST_ROOT, "etc/os-release")) as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except Exception:
        pass
    return ""


DEMO_SYSTEM = {
    "demo": True, "hostname": "homeserver", "os": "Ubuntu Server", "cpu_model": "Example 4-core CPU",
    "cores": 4, "threads": 8, "cpu": 23.0, "per_thread": [41, 18, 35, 12, 29, 9, 22, 14],
    "busy_threads": 1.8, "load": [1.92, 1.71, 1.55], "mem_used": 11.4, "mem_total": 32.0,
    "mem_cache": 14.2, "uptime_days": 41.2,
}


def system():
    if psutil is None:
        return dict(DEMO_SYSTEM)
    try:
        per = psutil.cpu_percent(interval=0.3, percpu=True)
        threads = len(per) or (os.cpu_count() or 1)
        total = sum(per) / threads
        vm = psutil.virtual_memory()
        cache = getattr(vm, "cached", 0) + getattr(vm, "buffers", 0)
        try:
            load = list(os.getloadavg())
        except Exception:
            load = []
        return {
            "demo": False,
            "hostname": _hostname(),
            "os": _os_name(),
            "cpu_model": _cpuinfo_model(),
            "cores": psutil.cpu_count(logical=False) or threads,
            "threads": threads,
            "cpu": round(total, 1),
            "per_thread": [round(p) for p in per],
            "busy_threads": round(total * threads / 100, 1),
            "load": [round(x, 2) for x in load],
            "mem_used": round((vm.total - vm.available) / 1073741824, 1),
            "mem_total": round(vm.total / 1073741824, 1),
            "mem_cache": round(cache / 1073741824, 1),
            "uptime_days": round((time.time() - psutil.boot_time()) / 86400, 1),
        }
    except Exception:
        return dict(DEMO_SYSTEM)


# ------------------------------------------------------------------ network --
_SKIP = re.compile(r"^(lo|docker\d*|br-|veth|tailscale|virbr|cni|flannel|zt|wg)")
_net_prev = {}
_net_rate = {}
_net_lock = threading.Lock()


def _sysnet():
    p = os.path.join(HOST_ROOT, "sys/class/net")
    return p if os.path.isdir(p) else "/sys/class/net"


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except Exception:
        return None


def _pick_iface(base):
    iface = (settings.get("net_iface") or "").strip()
    if iface:
        return iface
    best, best_rx = None, -1
    try:
        for name in os.listdir(base):
            if _SKIP.match(name):
                continue
            rx = _read_int(os.path.join(base, name, "statistics/rx_bytes")) or 0
            if rx > best_rx:
                best, best_rx = name, rx
    except Exception:
        pass
    return best


def network():
    base = _sysnet()
    iface = _pick_iface(base)
    if not iface:
        return {"enabled": False}
    rx = _read_int(os.path.join(base, iface, "statistics/rx_bytes"))
    tx = _read_int(os.path.join(base, iface, "statistics/tx_bytes"))
    if rx is None or tx is None:
        return {"enabled": False}
    speed = _read_int(os.path.join(base, iface, "speed"))
    if iface not in _net_prev:  # first call: take a quick baseline so we can report a rate
        with _net_lock:
            _net_prev[iface] = (time.time(), rx, tx)
        time.sleep(0.6)
        rx = _read_int(os.path.join(base, iface, "statistics/rx_bytes")) or rx
        tx = _read_int(os.path.join(base, iface, "statistics/tx_bytes")) or tx
    now = time.time()
    with _net_lock:
        prev = _net_prev.get(iface)
        down, up = _net_rate.get(iface, (None, None))
        if prev and now - prev[0] > 0.5:  # too-close calls reuse the last rate
            dt = now - prev[0]
            down = round(max(rx - prev[1], 0) * 8 / dt / 1e6, 1)
            up = round(max(tx - prev[2], 0) * 8 / dt / 1e6, 1)
            _net_rate[iface] = (down, up)
            _net_prev[iface] = (now, rx, tx)
    return {"enabled": True, "iface": iface, "link_mbps": speed if speed and speed > 0 else None,
            "down_mbps": down, "up_mbps": up, "host_view": base.startswith(HOST_ROOT)}


# ---------------------------------------------------------------- tailscale --
class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path, timeout=3):
        super().__init__("local-tailscaled.sock", timeout=timeout)
        self._path = path

    def connect(self):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect(self._path)
        self.sock = s


def tailscale():
    sock = ts_socket()
    if not sock or not os.path.exists(sock):
        return {"enabled": False}
    try:
        conn = _UnixHTTP(sock)
        conn.request("GET", "/localapi/v0/status", headers={"Host": "local-tailscaled.sock"})
        r = conn.getresponse()
        st = json.loads(r.read().decode())
        conn.close()
        me = st.get("Self") or {}
        peers = list((st.get("Peer") or {}).values())
        routes = [x for x in (me.get("PrimaryRoutes") or []) if x not in ("0.0.0.0/0", "::/0")]
        using_exit = next((p.get("HostName") for p in peers if p.get("ExitNode")), None)
        return {
            "enabled": True,
            "backend": st.get("BackendState"),
            "online": bool(me.get("Online")) and st.get("BackendState") == "Running",
            "hostname": me.get("HostName"),
            "ip": (me.get("TailscaleIPs") or [None])[0],
            "exit_node_offered": bool(me.get("ExitNodeOption")),
            "using_exit_node": using_exit,
            "routes": routes,
            "peers_online": sum(1 for p in peers if p.get("Online")),
            "peers_total": len(peers),
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}


# ----------------------------------------------------------------- adguard --
def _ag(path, url=None, user=None, pw=None):
    url = (url if url is not None else settings.get("adguard_url") or "").rstrip("/")
    user = user if user is not None else settings.get("adguard_user")
    pw = pw if pw is not None else settings.get("adguard_password")
    req = urllib.request.Request(url + path, headers={"Accept": "application/json"})
    if user:
        tok = base64.b64encode(f"{user}:{pw or ''}".encode()).decode()
        req.add_header("Authorization", "Basic " + tok)
    with urllib.request.urlopen(req, timeout=4) as r:
        return json.loads(r.read().decode())


def adguard(url=None, user=None, pw=None):
    if not (url or settings.get("adguard_url")):
        return {"enabled": False}
    try:
        st = _ag("/control/status", url, user, pw)
        stats = _ag("/control/stats", url, user, pw)
        q = stats.get("num_dns_queries") or 0
        b = stats.get("num_blocked_filtering") or 0
        hours = None
        if stats.get("time_units") == "hours":
            hours = len(stats.get("dns_queries") or []) or None
        return {"enabled": True, "protection": bool(st.get("protection_enabled")),
                "running": bool(st.get("running", True)), "queries": q, "blocked": b,
                "blocked_pct": round(b / q * 100, 1) if q else 0.0, "window_hours": hours}
    except Exception as e:
        return {"enabled": True, "error": str(e)}
