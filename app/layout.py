"""Server-side persistence of the dashboard layout (panel order, container
order, and groups) so it follows you across browsers/devices. Stored as JSON
on the /data volume. Falls back silently if the volume isn't writable.
"""
import os
import json

_PATH = os.path.join(os.environ.get("JARVIS_DATA", "/data"), "layout.json")


def load():
    try:
        with open(_PATH) as f:
            return json.load(f)
    except Exception:
        return None


def save(data):
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, _PATH)
        return True
    except Exception:
        return False
