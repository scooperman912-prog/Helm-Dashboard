"""Helm — home-server dashboard backend.

Public:    /api/health, /api/me, POST /api/login, POST /api/logout
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

try:
    import psutil
except Exception:  # pragma: no cover
    psutil = None

VERSION = "2.0.0"
app = FastAPI(title="Helm", version=VERSION)
HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_SECURE = os.environ.get("JARVIS_COOKIE_SECURE", "0") == "1"
ASSISTANT_DEFAULT = os.environ.get("ASSISTANT_ENABLED", "1") == "1"
BACKUP_STATUS = os.environ.get("BACKUP_STATUS", "").strip()
BACKUP_COMMAND = os.environ.get("BACKUP_COMMAND", "~/helm-backups/run-backup.sh")

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


def _parse_drives():
    """DRIVES: 'Name:/path,Other:/path2' (paths as seen inside the container)."""
    out = []
    for item in os.environ.get("DRIVES", "").split(","):
        item = item.strip()
        if not item:
            continue
        name, path = item.split(":", 1) if ":" in item else (item, item)
        out.append((name.strip(), path.strip()))
    return out


def storage_data():
    drives = _parse_drives()
    if psutil is not None and drives:
        out = []
        root = hostinfo.HOST_ROOT.rstrip("/")
        for name, path in drives:
            try:
                u = psutil.disk_usage(path)
                shown = path[len(root):] or "/" if path.startswith(root) else path
                out.append({"name": name, "mount": shown, "used": round(u.used / 1e9),
                            "total": round(u.total / 1e9), "pct": round(u.percent)})
            except Exception:
                continue
        if out:
            return {"demo": False, "drives": out}
    return {"demo": True, "drives": DEMO_DRIVES}


def backups_data():
    if not BACKUP_STATUS:
        return {"ok": False, "configured": False}
    try:
        with open(BACKUP_STATUS) as f:
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


# --------------------------------------------------------------------- auth --
def require_auth(request: Request):
    if auth.auth_required() and not auth.valid_token(request.cookies.get(auth.COOKIE)):
        raise HTTPException(status_code=401, detail="login required")


class LoginBody(BaseModel):
    password: str = ""


@app.get("/api/health")
def health():
    return {"ok": True, "version": VERSION}


@app.get("/api/me")
def me(request: Request):
    required = auth.auth_required()
    authed = (not required) or auth.valid_token(request.cookies.get(auth.COOKIE))
    cfg = {}
    if authed:
        cfg = {"version": VERSION, "assistant_default": ASSISTANT_DEFAULT, "assistant": assistant.info(),
               "access_tracking": access.tracking_enabled(), "backup_command": BACKUP_COMMAND}
    return {"auth_required": required, "authed": authed, **cfg}


@app.post("/api/login")
def login(body: LoginBody, response: Response):
    if not auth.auth_required():
        return {"ok": True, "note": "auth disabled"}
    if not auth.check_password(body.password):
        time.sleep(0.5)
        return JSONResponse({"ok": False, "error": "Wrong password"}, status_code=401)
    response.set_cookie(auth.COOKIE, auth.make_token(), httponly=True,
                        samesite="lax", secure=COOKIE_SECURE, max_age=7 * 24 * 3600, path="/")
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


def _err(e, code=400):
    return JSONResponse({"ok": False, "error": str(e)}, status_code=code)


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
