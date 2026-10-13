"""Lightweight session auth. If JARVIS_PASSWORD is set, a login is required and
data endpoints are protected by a signed, expiring cookie token. If it's unset,
auth is disabled (handy for a first LAN test). No external dependencies — the
token is an HMAC over an expiry timestamp using a per-install secret.
"""
import os
import hmac
import time
import hashlib
import secrets

COOKIE = "jarvis_session"  # name kept so existing logins survive the upgrade
_TTL = 7 * 24 * 3600  # 7 days
_PW = os.environ.get("JARVIS_PASSWORD") or None
_secret_cache = None


def auth_required():
    return _PW is not None


def _secret():
    global _secret_cache
    if _secret_cache:
        return _secret_cache
    path = os.path.join(os.environ.get("JARVIS_DATA", "/data"), ".session_secret")
    try:
        if os.path.exists(path):
            with open(path, "rb") as f:
                _secret_cache = f.read()
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _secret_cache = secrets.token_bytes(32)
            with open(path, "wb") as f:
                f.write(_secret_cache)
    except Exception:
        _secret_cache = secrets.token_bytes(32)  # ephemeral fallback
    return _secret_cache


def make_token():
    exp = str(int(time.time()) + _TTL)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return exp + "." + sig


def valid_token(tok):
    if not tok or "." not in tok:
        return False
    exp, sig = tok.split(".", 1)
    good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        return False
    try:
        return int(exp) > time.time()
    except Exception:
        return False


def check_password(pw):
    return _PW is not None and hmac.compare_digest((pw or "").encode(), _PW.encode())
