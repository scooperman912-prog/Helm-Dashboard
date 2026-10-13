# Helm

A calm, single-page dashboard for a self-hosted Docker home server: services, storage, network, alerts, files, and an optional local AI assistant.

Helm runs as one small container (FastAPI + a plain-JS page). It talks to Docker only through a locked-down socket proxy.

## Features

- **Services board.** Every container, grouped by Docker Compose stack, with an **Open ↗** link to each app.
  - Collapsed stacks show **total CPU and RAM for the whole stack**.
  - Expand a stack to see its containers. Click any container for a detail pop-up: online for, last accessed, CPU now and 24 h peak, memory, restarts, health, ports, network I/O, mounts, logs.
  - Sort by CPU, RAM or name, or switch to custom order to drag cards and drop one onto another to make your own groups.
- **Clear CPU numbers.** The host figure is the whole server (all threads = 100 %), with a bar per thread. Container figures are a *share of the whole server*, so they add up to the host number. Each also shows the same load in threads ("0.75 thr").
- **Needs attention.** Drives filling up, available updates, crashed, unhealthy or paused containers, and overdue or failed backups.
  - Dismiss an alert (it comes back if it gets worse), silence it for 1 hour or 1 day, until it changes, or for good.
  - Clear all, restore silenced alerts, or mute everything. Optional phone pushes via [ntfy](https://ntfy.sh).
- **Storage.** Per-drive usage bars, plus backup status if your backup script writes a status file.
- **Network.** Live throughput on the host's main interface, Tailscale status (online, exit node, routes, peers), and AdGuard Home stats.
- **Files.** Browse, rename, move, delete and create folders, limited to the folders you allow.
- **Safe updates.** Update detection via [What's Up Docker](https://github.com/fmartinou/whats-up-docker). Helm writes a copy-paste plan that dumps the stack's databases first, then pulls and restarts.
- **Assistant (optional).** Ask a question in the bar under *Needs attention*. The page glides away into a chat view, and a **Helm / Chat** switch lets you flip back and forth.
  - Point it at any OpenAI-compatible local server (Ollama, llama.cpp, LM Studio). Each question includes a live snapshot of your server.
  - With no model configured it runs in **preview mode**, giving short answers built from live data.
  - Turn it on or off from the header.
- Light and dark themes, a password login, and a layout that works on a phone.

## Quick start

```bash
git clone https://github.com/scooperman912-prog/helm-dashboard.git
cd helm-dashboard
cp .env.example .env                                          # set JARVIS_PASSWORD at least
cp docker-compose.override.example.yml docker-compose.override.yml   # your drives & folders
docker compose up -d --build
# open http://<server-ip>:8787
```

`docker-compose.yml` is generic. Your machine-specific paths go in `docker-compose.override.yml` and your secrets in `.env`. Both files are git-ignored.

## Configuration

| Variable | What it does |
|---|---|
| `JARVIS_PASSWORD` | Requires a login. Leave unset only for a quick LAN test. |
| `DRIVES` | `Name:/path,...`: drives to show (paths as seen inside the container). |
| `FILE_ROOTS` | `Name:/path,...`: folders the file manager may use. Each needs a read-write volume. |
| `WUD_URL` | What's Up Docker API, for update flags. |
| `NTFY_URL`, `NTFY_TOKEN` | Phone pushes for new alerts. `ALERT_INTERVAL` sets how often to check (seconds, default 300). |
| `BACKUP_STATUS`, `BACKUP_COMMAND`, `BACKUP_DIR` | Backup status file, the "Back up now" command, and the folder for Safe Update DB dumps. `BACKUP_MAX_AGE_HOURS` (default 192) sets when backups count as overdue. |
| `NPM_DATA_DIR` | Nginx Proxy Manager's `/data`, mounted read-only. Enables **last accessed** and automatic **Open** links. |
| `SERVICE_URLS` | `name=url,...`: links for anything NPM doesn't cover. A container label `helm.url=` also works. |
| `TAILSCALE_SOCKET` | Path to `tailscaled.sock` (default `/var/run/tailscale/tailscaled.sock`; mount `/var/run/tailscale` read-only). |
| `ADGUARD_URL`, `ADGUARD_USER`, `ADGUARD_PASSWORD` | AdGuard Home panel. |
| `NET_IFACE` | Network interface for the throughput panel (auto-detected otherwise). |
| `ASSISTANT_URL`, `ASSISTANT_MODEL`, `ASSISTANT_API_KEY`, `ASSISTANT_NAME`, `ASSISTANT_ENABLED` | Assistant settings. Without a URL it runs in preview mode. |
| `JARVIS_COOKIE_SECURE` | Set to `1` when serving over HTTPS. |

The `JARVIS_` prefix is kept from Helm's original name so older installs keep working.

## Security model

- Helm never opens the Docker socket itself. `tecnativa/docker-socket-proxy` allows only container list, inspect, stats, logs and start/stop/restart/pause, plus info. It has no exec, image, network or volume access, and publishes no ports.
- The host's `/` is mounted **read-only**. The only writable paths are the file-manager folders you list.
- File operations are confined to `FILE_ROOTS`, with realpath checks: anything outside a root is refused, and roots themselves can't be deleted.
- The app runs as a non-root user. Every data endpoint needs the login cookie when a password is set.
- Helm is meant to stay on your LAN or tailnet. Don't expose it to the internet.
- The assistant sends your server snapshot only to the `ASSISTANT_URL` you configure.

## Layout

```
app/
  main.py            API routes + auth gate
  docker_backend.py  containers via the socket proxy, background sampler, WUD, safe updates
  hostinfo.py        CPU/memory, host network, Tailscale, AdGuard
  alerts.py          Needs-attention feed, silencing, ntfy pushes
  access.py          NPM "last accessed" + service links
  assistant.py       chat endpoint (local model or preview)
  files.py           confined file operations
  layout.py, auth.py
  static/index.html  the whole UI
```
