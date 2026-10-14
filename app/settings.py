"""Helm's configuration, editable from the browser (setup wizard / Settings).

Values live in JARVIS_DATA/settings.json. Anything not set there falls back to
the matching environment variable, so compose/.env-based installs keep working.
Every module reads settings through get() at call time, so changes made in the
UI apply immediately — no restart needed.
"""
import os
import json
import threading

_PATH = os.path.join(os.environ.get("JARVIS_DATA", "/data"), "settings.json")
_lock = threading.Lock()
_cache = {"mtime": None, "data": {}}

HOST_ROOT = os.environ.get("HOST_ROOT", "/host/root").rstrip("/") or "/"


def _pairs(raw):
    """'Name:/path,Other:/p2' -> [{"name","path"}]"""
    out = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, path = item.split(":", 1) if ":" in item else (os.path.basename(item.rstrip("/")) or item, item)
        out.append({"name": name.strip(), "path": path.strip()})
    return out


def _env_bool(name, default):
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


# key -> zero-arg function giving the environment default
ENV_DEFAULTS = {
    "drives": lambda: _pairs(os.environ.get("DRIVES", "")),
    "file_roots": lambda: _pairs(os.environ.get("FILE_ROOTS", "")),
    "wud_url": lambda: os.environ.get("WUD_URL", ""),
    "adguard_url": lambda: os.environ.get("ADGUARD_URL", ""),
    "adguard_user": lambda: os.environ.get("ADGUARD_USER", ""),
    "adguard_password": lambda: os.environ.get("ADGUARD_PASSWORD", ""),
    "npm_data_dir": lambda: os.environ.get("NPM_DATA_DIR", ""),
    "tailscale_socket": lambda: os.environ.get("TAILSCALE_SOCKET", ""),
    "net_iface": lambda: os.environ.get("NET_IFACE", ""),
    "ntfy_url": lambda: os.environ.get("NTFY_URL", ""),
    "ntfy_token": lambda: os.environ.get("NTFY_TOKEN", ""),
    "backup_status": lambda: os.environ.get("BACKUP_STATUS", ""),
    "backup_command": lambda: os.environ.get("BACKUP_COMMAND", "~/helm-backups/run-backup.sh"),
    "backup_dir": lambda: os.environ.get("BACKUP_DIR", "~/helm-backups"),
    "service_urls": lambda: os.environ.get("SERVICE_URLS", ""),
    "assistant_enabled": lambda: _env_bool("ASSISTANT_ENABLED", True),
    "assistant_name": lambda: os.environ.get("ASSISTANT_NAME", "Cubert"),
    "assistant_url": lambda: os.environ.get("ASSISTANT_URL", ""),
    "assistant_model": lambda: os.environ.get("ASSISTANT_MODEL", ""),
    "assistant_api_key": lambda: os.environ.get("ASSISTANT_API_KEY", ""),
    "password_hash": lambda: "",
    "setup_done": lambda: False,
}

SECRET_KEYS = {"adguard_password", "ntfy_token", "assistant_api_key", "password_hash"}


def _load():
    try:
        m = os.path.getmtime(_PATH)
    except OSError:
        return {}
    if m != _cache["mtime"]:
        try:
            with open(_PATH) as f:
                _cache["data"] = json.load(f)
        except Exception:
            _cache["data"] = {}
        _cache["mtime"] = m
    return _cache["data"]


def get(key):
    with _lock:
        s = _load()
    v = s.get(key)
    if v is None or v == "" or v == []:
        f = ENV_DEFAULTS.get(key)
        return f() if f else None
    return v


def update(values):
    """Merge `values` into settings.json. A value of None removes the key."""
    with _lock:
        s = dict(_load())
        for k, v in values.items():
            if k not in ENV_DEFAULTS:
                continue
            if v is None:
                s.pop(k, None)
            else:
                s[k] = v
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(s, f, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _PATH)
        _cache["mtime"] = None


def public():
    """All effective settings for the Settings UI — secrets are never sent back,
    only whether they're set."""
    out = {}
    for k in ENV_DEFAULTS:
        if k == "password_hash":
            continue
        v = get(k)
        out[k] = {"set": bool(v)} if k in SECRET_KEYS else v
    return out


def show_path(p):
    """Container path -> the host path a person recognises."""
    if p and HOST_ROOT != "/" and (p == HOST_ROOT or p.startswith(HOST_ROOT + "/")):
        return p[len(HOST_ROOT):] or "/"
    return p
