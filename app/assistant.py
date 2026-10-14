"""Cubert — the optional chat assistant.

If ASSISTANT_URL is set it talks to any OpenAI-compatible chat endpoint
(Ollama, llama.cpp server, LM Studio, vLLM...):
    ASSISTANT_URL=http://ollama:11434/v1
    ASSISTANT_MODEL=llama3.1:8b
    ASSISTANT_API_KEY=            (optional)
Each request includes a compact snapshot of the server so the model can answer
questions about it. Nothing leaves your network unless you point it elsewhere.

Without ASSISTANT_URL it runs in preview mode: short rule-based answers built
from the same live data, so the chat UI can be used and tested today.
"""
import os
import json
import urllib.request

import settings


def _cfg():
    return ((settings.get("assistant_url") or "").rstrip("/"), settings.get("assistant_model") or "",
            settings.get("assistant_api_key") or "", settings.get("assistant_name") or "Cubert")


TIMEOUT = int(os.environ.get("ASSISTANT_TIMEOUT", "120"))


def info():
    url, model, _, name = _cfg()
    return {"name": name, "connected": bool(url and model), "model": model or None}


def _snapshot(ctx):
    sysd, cs, drives, net, ts, alerts = (ctx.get(k) or {} for k in ("system", "containers", "drives", "network", "tailscale", "alerts"))
    top = sorted(cs.get("containers", []), key=lambda c: -c.get("cpu", 0))
    return {
        "host": {k: sysd.get(k) for k in ("hostname", "os", "cpu_model", "threads", "cpu", "busy_threads", "load",
                                           "mem_used", "mem_total", "uptime_days")},
        "containers": [{"name": c["name"], "stack": c.get("project"), "status": c["status"],
                        "cpu_pct_of_server": c.get("cpu"), "threads": c.get("cpu_thr"), "ram_mb": c.get("mem_used"),
                        "update": c.get("new_ver") if c.get("update") else None} for c in top],
        "drives": drives.get("drives", []),
        "network": net, "tailscale": ts,
        "alerts": [{"title": a["title"], "detail": a["detail"]} for a in alerts.get("alerts", [])],
    }


def _system_prompt(ctx):
    NAME = _cfg()[3]
    return (f"You are {NAME}, the assistant built into Helm, a self-hosted dashboard for a home server. "
            "Answer briefly and concretely using the live snapshot below. CPU numbers are percent of the whole "
            "server (all threads = 100%); 'threads' is the same load in threads. If something needs a command, "
            "give the exact command and say what it does. Flag anything risky (exposing ports, deleting data) "
            "before suggesting it. If the snapshot doesn't contain the answer, say so.\n\nSNAPSHOT:\n"
            + json.dumps(_snapshot(ctx), separators=(",", ":"), default=str))


def _call_model(messages, ctx, url=None, model=None, key=None):
    cu, cm, ck, _ = _cfg()
    URL, MODEL, KEY = (url or cu).rstrip("/"), model or cm, key if key is not None else ck
    body = {"model": MODEL, "stream": False,
            "messages": [{"role": "system", "content": _system_prompt(ctx)}] + messages[-12:]}
    req = urllib.request.Request(URL + "/chat/completions", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    if KEY:
        req.add_header("Authorization", "Bearer " + KEY)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        data = json.loads(r.read().decode())
    return data["choices"][0]["message"]["content"].strip()


# ------------------------------------------------------------- preview mode --
def _gb(x):
    return f"{x / 1000:.2f} TB" if x >= 1000 else f"{round(x)} GB"


def _preview(q, ctx):
    s = q.lower()
    snap = _snapshot(ctx)
    if any(k in s for k in ("stor", "space", "disk", "drive", "full")):
        ds = sorted(snap["drives"], key=lambda d: -d.get("pct", 0))
        if not ds:
            return "I can't see any drives yet — set DRIVES in the compose file."
        lines = [f"• {d['name']}: {_gb(d['used'])} of {_gb(d['total'])} ({d['pct']}%) — {_gb(d['total'] - d['used'])} free" for d in ds]
        return f"{ds[0]['name']} is the fullest.\n\n" + "\n".join(lines)
    if any(k in s for k in ("updat", "version", "upgrade")):
        ups = [c for c in snap["containers"] if c["update"]]
        if not ups:
            return "Everything is up to date."
        return "Updates waiting:\n" + "\n".join(f"• {c['name']} → {c['update']}" for c in ups) + \
            "\n\nUse Helm's Safe Update plan — it backs up any database in the stack first."
    if any(k in s for k in ("tailscale", "network", "vpn", "exit", "adguard", "dns", "internet", "speed")):
        n, t = snap["network"] or {}, snap["tailscale"] or {}
        parts = []
        if n.get("enabled"):
            parts.append(f"Traffic on {n.get('iface')}: ↓ {n.get('down_mbps')} Mbps, ↑ {n.get('up_mbps')} Mbps.")
        if t.get("enabled") and not t.get("error"):
            parts.append(f"Tailscale is {'online' if t.get('online') else 'offline'}"
                         f"{', offering an exit node' if t.get('exit_node_offered') else ''}; "
                         f"{t.get('peers_online')} of {t.get('peers_total')} peers online.")
        return " ".join(parts) or "Network panels aren't configured yet."
    h = snap["host"]
    busy = [c for c in snap["containers"] if c["status"] == "running"][:3]
    lines = [f"• {c['name']}: {c['cpu_pct_of_server']:.1f}% ({c['threads']:.2f} threads), {c['ram_mb']} MB" for c in busy]
    alerts = snap["alerts"]
    tail = f"\n\n{len(alerts)} thing{'s' if len(alerts) != 1 else ''} in Needs attention." if alerts else "\n\nNothing needs attention."
    return (f"The server is at {h.get('cpu')}% CPU — about {h.get('busy_threads')} of {h.get('threads')} threads busy — "
            f"with {h.get('mem_used')} of {h.get('mem_total')} GB RAM in use.\n\nBusiest right now:\n"
            + "\n".join(lines) + tail)


def chat(messages, ctx):
    msgs = [{"role": m.get("role"), "content": str(m.get("content", ""))[:4000]}
            for m in messages if m.get("role") in ("user", "assistant")]
    if not msgs or msgs[-1]["role"] != "user":
        raise ValueError("last message must be from the user")
    URL, MODEL = _cfg()[:2]
    if URL and MODEL:
        try:
            return {"reply": _call_model(msgs, ctx), "preview": False}
        except Exception as e:
            return {"reply": f"I couldn't reach the model at ASSISTANT_URL ({e}). Here's a quick answer from live data instead:\n\n"
                    + _preview(msgs[-1]["content"], ctx), "preview": True}
    return {"reply": _preview(msgs[-1]["content"], ctx), "preview": True}
