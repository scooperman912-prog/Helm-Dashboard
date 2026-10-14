"""Helm — home-server dashboard backend.

Public:    /api/health, /api/me, POST /api/login, POST /api/logout,
           POST /api/setup/start {code, password}   (only before a password exists)
Protected (login required when JARVIS_PASSWORD is set):
  containers  GET  /api/containers               GET  /api/containers/{id}/logs
              POST /api/containers/{id}/{act}    GET  /api/containers/{id}/update-plan
              POST /api/stacks/{project}/{act}
  host        GET  /api/system  /api/network  /api/storage  /api/backups
  alerts      GET  /api/alerts                   POST /api/alerts/{id}/{dismiss|restore}
              POST /api/alerts/{id}/snooze {mode}  POST /api/alerts/clear  POST /api/alerts/mute {muted}
  assistant   POST /api/assistant {messages}
  files       GET  /api/files/roots  /api/files/list   POST /api/files/{rename,move,mkdir,delete}
  layout      GET/PUT /api/layout
  settings    GET/PUT /api/settings  GET /api/setup/detect  POST /api/settings/test/{kind}
              POST /api/settings/password  GET /api/setup/ntfy-topic  POST /api/setup/write-snippet
Static UI is served from ./static at /.
"""
import os
import copy
import json
import time
import concurrent.futures

from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import docker_backend as db
import hostinfo
import access
import alerts
import assistant
import files as fm
import layout as lay
import auth
import settings
import setup as wizard

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

VERSION = "2.1.0"
app = FastAPI(title="Helm", version=VERSION)
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_SECURE = os.environ.get("JARVIS_COOKIE_SECURE", "0") == "1"

DEMO_DRIVES = [
    {"name": "System", "mount": "/", "used": 412, "total": 936, "pct": 44},
    {"name": "Media", "mount": "/srv/media", "used": 1520, "total": 1900, "pct": 80},
    {"name": "Backup", "mount": "/mnt/backup", "used": 1510, "total": 1800, "pct": 84},
    {"name": "Photos", "mount": "/mnt/photos", "used": 1900, "total": 3600, "pct": 53},
]


# ---------------------------------------------------------------- data fns --
def containers_data():
    d = db.list_containers()
    d = dict(d, containers=copy.deepcopy(d["containers"]))
    if not d["demo"]:
        access.enrich(d["containers"])
    return d


def _drives():
    """Drives chosen in Settings (or DRIVES env). Unconfigured: just the system drive."""
    ds = [(d.get("name") or d.get("path"), d.get("path")) for d in settings.get("drives") or [] if d.get("path")]
    if not ds and os.path.isdir(settings.HOST_ROOT):
        ds = [("System", settings.HOST_ROOT)]
    return ds


def storage_data():
    drives = _drives()
    if psutil is not None and drives:
        out = []
        for name, path in drives:
            try:
                u = psutil.disk_usage(path)
                out.append({"name": name, "mount": settings.show_path(path), "used": round(u.used / 1e9),
                            "total": round(u.total / 1e9), "pct": round(u.percent)})
            except Exception:
                continue
        if out:
            return {"demo": False, "drives": out}
    return {"demo": True, "drives": DEMO_DRIVES}


def backups_data():
    path = settings.get("backup_status")
    if not path:
        return {"ok": False, "configured": False}
    try:
        with open(path) as f:
            return {"ok": True, "configured": True, **json.load(f)}
    except Exception as e:
        return {"ok": False, "configured": True, "error": str(e)}


alerts.configure(containers=lambda: containers_data()["containers"],
                 drives=lambda: storage_data()["drives"],
                 backups=backups_data)


@app.on_event("startup")
def _startup():
    db.start_sampler()
    alerts.start_notifier()
    if not auth.has_password():
        code = auth.setup_code()
        bar = "=" * 60
        print(f"\n{bar}\n  Helm is ready for setup.\n  Open it in your browser and enter this setup code:\n\n"
              f"      {code}\n\n  (shown until a password is created)\n{bar}\n", flush=True)


# --------------------------------------------------------------------- auth --
def require_auth(request: Request):
    if not auth.has_password():
        raise HTTPException(status_code=401, detail="setup required")
    if not auth.valid_token(request.cookies.get(auth.COOKIE)):
        raise HTTPException(status_code=401, detail="login required")


def _secure(request):
    return COOKIE_SECURE or request.url.scheme == "https" or \
        request.headers.get("x-forwarded-proto", "").lower() == "https"


def _set_session(request, response):
    response.set_cookie(auth.COOKIE, auth.make_token(), httponly=True, samesite="lax",
                        secure=_secure(request), max_age=7 * 24 * 3600, path="/")


class LoginBody(BaseModel):
    password: str = ""


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/me")
def me(request: Request):
    has_pw = auth.has_password()
    authed = has_pw and auth.valid_token(request.cookies.get(auth.COOKIE))
    out = {"auth_required": True, "authed": authed, "needs_password": not has_pw,
           "setup_done": bool(settings.get("setup_done")), "env_password": auth.env_password()}
    if authed:
        out.update({"version": VERSION, "assistant_default": bool(settings.get("assistant_enabled")),
                    "assistant": assistant.info(), "access_tracking": access.tracking_enabled(),
                    "backup_command": settings.get("backup_command")})
    return out


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    if not auth.has_password():
        return JSONResponse({"ok": False, "error": "Finish setup first"}, status_code=409)
    if not auth.check_password(body.password):
        time.sleep(0.5)
        return JSONResponse({"ok": False, "error": "Wrong password"}, status_code=401)
    _set_session(request, response)
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


def _err(e, code=400):
    return JSONResponse({"ok": False, "error": str(e)}, status_code=code)


# ------------------------------------------------------------ setup/settings --
class StartBody(BaseModel):
    code: str = ""
    password: str = ""


@app.post("/api/setup/start")
def setup_start(b: StartBody, request: Request, response: Response):
    """Create the admin password. Only possible while no password exists, and
    only with the one-time code from the container log."""
    if auth.has_password():
        return JSONResponse({"ok": False, "error": "A password already exists — sign in instead"}, status_code=409)
    if not auth.check_setup_code(b.code):
        time.sleep(0.5)
        return JSONResponse({"ok": False, "error": "That setup code doesn't match the one in the log"}, status_code=403)
    if len(b.password) < 8:
        return JSONResponse({"ok": False, "error": "Use at least 8 characters"}, status_code=400)
    settings.update({"password_hash": auth.hash_password(b.password)})
    _set_session(request, response)
    return {"ok": True}


@app.get("/api/setup/detect", dependencies=[Depends(require_auth)])
def setup_detect():
    return wizard.detect()


@app.get("/api/settings", dependencies=[Depends(require_auth)])
def get_settings():
    return {"settings": settings.public(), "host_root": settings.HOST_ROOT, "env_password": auth.env_password()}


_EDITABLE = set(settings.ENV_DEFAULTS) - {"password_hash"}


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def put_settings(request: Request):
    body = await request.json()
    vals = {}
    for k, v in (body.get("settings") or {}).items():
        if k not in _EDITABLE:
            continue
        if k in settings.SECRET_KEYS and v == "":
            continue  # blank secret field = keep the stored one
        vals[k] = v
    settings.update(vals)
    return {"ok": True, "settings": settings.public()}


class PasswordBody(BaseModel):
    current: str = ""
    new: str = ""


@app.post("/api/settings/password", dependencies=[Depends(require_auth)])
def change_password(b: PasswordBody):
    if auth.env_password():
        return _err("Your password is set by JARVIS_PASSWORD in .env — change it there", 409)
    if not auth.check_password(b.current):
        time.sleep(0.5)
        return _err("Current password is wrong", 403)
    if len(b.new) < 8:
        return _err("Use at least 8 characters")
    settings.update({"password_hash": auth.hash_password(b.new)})
    return {"ok": True}


@app.post("/api/settings/test/{kind}", dependencies=[Depends(require_auth)])
async def test_setting(kind: str, request: Request):
    b = await request.json()
    import anyio
    fns = {
        "wud": lambda: wizard.test_wud(b.get("url", "")),
        "adguard": lambda: wizard.test_adguard(b.get("url", ""), b.get("user", ""), b.get("password") or None),
        "npm": lambda: wizard.test_npm(b.get("path", "")),
        "ntfy": lambda: wizard.test_ntfy(b.get("url", ""), b.get("token") or settings.get("ntfy_token")),
        "assistant": lambda: wizard.test_assistant(b.get("url", ""), b.get("model", ""),
                                                   b.get("key") or settings.get("assistant_api_key")),
    }
    if kind not in fns:
        return _err("unknown test")
    return await anyio.to_thread.run_sync(fns[kind])


@app.get("/api/setup/ntfy-topic", dependencies=[Depends(require_auth)])
def ntfy_topic():
    return {"url": wizard.new_ntfy_topic()}


class SnippetBody(BaseModel):
    paths: list


@app.post("/api/setup/write-snippet", dependencies=[Depends(require_auth)])
def write_snippet(b: SnippetBody):
    return {"snippet": wizard.write_snippet([str(p) for p in b.paths])}


# --------------------------------------------------------------- containers --
@app.get("/api/containers", dependencies=[Depends(require_auth)])
def containers():
    return containers_data()


@app.get("/api/containers/{cid}/update-plan", dependencies=[Depends(require_auth)])
def container_update_plan(cid: str):
    try:
        return db.update_plan(cid)
    except Exception as e:
        return _err(e)


@app.get("/api/containers/{cid}/logs", dependencies=[Depends(require_auth)])
def container_logs(cid: str, tail: int = 200):
    try:
        return PlainTextResponse(db.logs(cid, min(max(tail, 10), 2000)))
    except Exception as e:
        return _err(e)


@app.post("/api/containers/{cid}/{act}", dependencies=[Depends(require_auth)])
def container_action(cid: str, act: str):
    try:
        if act == "update":
            return db.update_container(cid)
        return db.action(cid, act)
    except Exception as e:
        return _err(e)


@app.post("/api/stacks/{project}/{act}", dependencies=[Depends(require_auth)])
def stack_action(project: str, act: str):
    try:
        return db.project_action(project, act)
    except Exception as e:
        return _err(e)


# --------------------------------------------------------------------- host --
@app.get("/api/system", dependencies=[Depends(require_auth)])
def system():
    return hostinfo.system()


@app.get("/api/network", dependencies=[Depends(require_auth)])
def network():
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        n, t, a = ex.submit(hostinfo.network), ex.submit(hostinfo.tailscale), ex.submit(hostinfo.adguard)
        return {"network": n.result(), "tailscale": t.result(), "adguard": a.result()}


@app.get("/api/storage", dependencies=[Depends(require_auth)])
def storage():
    return storage_data()


@app.get("/api/backups", dependencies=[Depends(require_auth)])
def backups():
    return backups_data()


# ------------------------------------------------------------------- alerts --
class SnoozeBody(BaseModel):
    mode: str


class MuteBody(BaseModel):
    muted: bool


@app.get("/api/alerts", dependencies=[Depends(require_auth)])
def get_alerts():
    return alerts.view()


@app.post("/api/alerts/clear", dependencies=[Depends(require_auth)])
def clear_alerts():
    alerts.clear_all()
    return alerts.view()


@app.post("/api/alerts/mute", dependencies=[Depends(require_auth)])
def mute_alerts(b: MuteBody):
    alerts.set_muted(b.muted)
    return alerts.view()


@app.post("/api/alerts/{aid:path}/snooze", dependencies=[Depends(require_auth)])
def snooze_alert(aid: str, b: SnoozeBody):
    try:
        alerts.snooze(aid, b.mode)
    except Exception as e:
        return _err(e)
    return alerts.view()


@app.post("/api/alerts/{aid:path}/dismiss", dependencies=[Depends(require_auth)])
def dismiss_alert(aid: str):
    alerts.dismiss(aid)
    return alerts.view()


@app.post("/api/alerts/{aid:path}/restore", dependencies=[Depends(require_auth)])
def restore_alert(aid: str):
    alerts.restore(aid)
    return alerts.view()


# ---------------------------------------------------------------- assistant --
@app.post("/api/assistant", dependencies=[Depends(require_auth)])
async def assistant_chat(request: Request):
    try:
        body = await request.json()
        msgs = body.get("messages") or []
        ctx = {"system": hostinfo.system(), "containers": containers_data(), "drives": storage_data(),
               "alerts": alerts.view()}
        net = network()
        ctx["network"], ctx["tailscale"] = net["network"], net["tailscale"]
        import anyio
        return await anyio.to_thread.run_sync(assistant.chat, msgs, ctx)
    except Exception as e:
        return _err(e)


# -------------------------------------------------------------------- files --
class PathBody(BaseModel):
    path: str


class RenameBody(BaseModel):
    path: str
    newname: str


class MoveBody(BaseModel):
    src: str
    dest: str


class MkdirBody(BaseModel):
    path: str
    name: str


def _file_err(e):
    return _err(e, 403 if isinstance(e, PermissionError) else 400)


@app.get("/api/files/roots", dependencies=[Depends(require_auth)])
def files_roots():
    return {"demo": not fm.has_roots(), "roots": fm.roots()}


@app.get("/api/files/list", dependencies=[Depends(require_auth)])
def files_list(path: str):
    try:
        return fm.listdir(path)
    except Exception as e:
        return _file_err(e)


@app.post("/api/files/rename", dependencies=[Depends(require_auth)])
def files_rename(b: RenameBody):
    try:
        return fm.rename(b.path, b.newname)
    except Exception as e:
        return _file_err(e)


@app.post("/api/files/move", dependencies=[Depends(require_auth)])
def files_move(b: MoveBody):
    try:
        return fm.move(b.src, b.dest)
    except Exception as e:
        return _file_err(e)


@app.post("/api/files/mkdir", dependencies=[Depends(require_auth)])
def files_mkdir(b: MkdirBody):
    try:
        return fm.mkdir(b.path, b.name)
    except Exception as e:
        return _file_err(e)


@app.post("/api/files/delete", dependencies=[Depends(require_auth)])
def files_delete(b: PathBody):
    try:
        return fm.delete(b.path)
    except Exception as e:
        return _file_err(e)


# ------------------------------------------------------------------- layout --
@app.get("/api/layout", dependencies=[Depends(require_auth)])
def get_layout():
    return {"layout": lay.load()}


@app.put("/api/layout", dependencies=[Depends(require_auth)])
async def put_layout(request: Request):
    data = await request.json()
    return {"ok": lay.save(data.get("layout") if isinstance(data, dict) else data)}


# static UI last so /api/* wins
app.mount("/", StaticFiles(directory=os.path.join(HERE, "static"), html=True), name="static")
