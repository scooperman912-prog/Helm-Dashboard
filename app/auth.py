"""Login. The password is either JARVIS_PASSWORD (env) or the one created in the
setup wizard (stored as a salted PBKDF2 hash in settings.json). Sessions are
signed, expiring cookies — an HMAC over the expiry using a per-install secret.

Before any password exists, Helm is in setup mode: only the setup endpoints
work, and they require the one-time setup code printed in the container log.
"""
import os
import hmac
import time
import hashlib
import secrets

import settings

COOKIE = "jarvis_session"  # name kept so existing logins survive upgrades
_TTL = 7 * 24 * 3600
_ENV_PW = os.environ.get("JARVIS_PASSWORD") or None
_secret_cache = None
_ITER = 240_000


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
            os.chmod(path, 0o600)
    except Exception:
        _secret_cache = secrets.token_bytes(32)
    return _secret_cache


def env_password():
    return _ENV_PW is not None


def has_password():
    return _ENV_PW is not None or bool(settings.get("password_hash"))


def auth_required():
    return has_password()


def hash_password(pw):
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), _ITER).hex()
    return f"pbkdf2${_ITER}${salt}${dk}"


def check_password(pw):
    pw = pw or ""
    if _ENV_PW is not None:
        return hmac.compare_digest(pw.encode(), _ENV_PW.encode())
    stored = settings.get("password_hash") or ""
    try:
        _, it, salt, dk = stored.split("$")
        cand = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), int(it)).hex()
        return hmac.compare_digest(cand, dk)
    except Exception:
        return False


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


# ---------------------------------------------------------------- setup code --
_setup_code = None


def setup_code():
    """One-time code shown in the container log; proves the person running the
    wizard has access to the server, not just the network."""
    global _setup_code
    if _setup_code is None:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        _setup_code = "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
    return _setup_code


def check_setup_code(code):
    return hmac.compare_digest((code or "").strip().upper().encode(), setup_code().encode())
