"""File-manager backend. All operations are confined to the roots declared in
the FILE_ROOTS env var, with realpath-based path-traversal protection.

Roots come from Settings (or the FILE_ROOTS env var: "Name:/path,Other:/path2").
Folders under a read-only mount can be browsed but not changed.
"""
import os
import shutil

import settings


def _parse_roots():
    roots = []
    for r in settings.get("file_roots") or []:
        rp = os.path.realpath((r.get("path") or "").strip())
        if rp and os.path.isdir(rp):
            roots.append({"name": (r.get("name") or os.path.basename(rp) or rp).strip(), "path": rp,
                          "writable": os.access(rp, os.W_OK)})
    return roots


def roots():
    return [{"name": r["name"], "path": r["path"], "writable": r["writable"],
             "shown": settings.show_path(r["path"])} for r in _parse_roots()]


def has_roots():
    return len(_parse_roots()) > 0


def _resolve(path):
    """Return realpath of `path` only if it sits inside an allowed root."""
    rp = os.path.realpath(path)
    for r in _parse_roots():
        if rp == r["path"] or rp.startswith(r["path"] + os.sep):
            return rp
    raise PermissionError("path is outside the allowed roots")


def _safe_name(name):
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError("invalid name")
    return name


def listdir(path):
    rp = _resolve(path)
    if not os.path.isdir(rp):
        raise ValueError("not a directory")
    entries = []
    with os.scandir(rp) as it:
        for e in it:
            try:
                st = e.stat(follow_symlinks=False)
                is_dir = e.is_dir(follow_symlinks=False)
            except Exception:
                continue
            entries.append({
                "name": e.name,
                "path": os.path.join(rp, e.name),
                "is_dir": is_dir,
                "size": 0 if is_dir else st.st_size,
                "mtime": int(st.st_mtime),
            })
    entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return {"path": rp, "entries": entries}


def rename(path, newname):
    rp = _resolve(path)
    _safe_name(newname)
    dst = _resolve(os.path.join(os.path.dirname(rp), newname))
    if os.path.exists(dst):
        raise FileExistsError("target already exists")
    os.rename(rp, dst)
    return {"ok": True, "path": dst}


def move(src, dest_dir):
    s = _resolve(src)
    d = _resolve(dest_dir)
    if not os.path.isdir(d):
        raise ValueError("destination is not a directory")
    dst = _resolve(os.path.join(d, os.path.basename(s)))
    if os.path.exists(dst):
        raise FileExistsError("target already exists")
    shutil.move(s, dst)
    return {"ok": True, "path": dst}


def mkdir(path, name):
    rp = _resolve(path)
    _safe_name(name)
    d = _resolve(os.path.join(rp, name))
    os.makedirs(d, exist_ok=False)
    return {"ok": True, "path": d}


def delete(path):
    rp = _resolve(path)
    if any(rp == r["path"] for r in _parse_roots()):
        raise PermissionError("refusing to delete a configured root")
    if os.path.isdir(rp) and not os.path.islink(rp):
        shutil.rmtree(rp)
    else:
        os.remove(rp)
    return {"ok": True}
