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

## Install

On your server (needs Docker with Compose v2):

```bash
curl -fsSL https://raw.githubusercontent.com/scooperman912-prog/helm-dashboard/main/install.sh | bash
```

The script downloads Helm to `~/helm`, starts it, and prints the address and a **one-time setup code**.

Open the address in your browser and the setup wizard walks you through the rest, in about two minutes:

1. **Account.** Enter the setup code and choose a password.
2. **Drives.** Helm lists the drives it found. Tick the ones to watch.
3. **Folders.** Pick folders for the file manager. They're view-only until you allow changes, and Helm shows the exact snippet to do that.
4. **Apps.** What's Up Docker, AdGuard Home, Nginx Proxy Manager, Tailscale and backups. Anything Helm finds is pre-filled; press **Test**.
5. **Alerts.** Generate a private ntfy topic and send a test push to your phone.
6. **Assistant.** Show or hide the ask bar, and optionally point it at a local model.

You can change any of this later from the **gear** icon.

- **Update:** run the same command again.
- **Change the port:** `HELM_PORT=8788 bash install.sh`, or run the curl command with `HELM_PORT=8788` set.
- **Uninstall:** `cd ~/helm && docker compose down -v && rm -rf ~/helm`

<details><summary>Manual install</summary>

```bash
git clone https://github.com/scooperman912-prog/helm-dashboard.git ~/helm
cd ~/helm
docker compose up -d --build
docker compose logs helm | grep -A2 "setup code"   # then open http://<server-ip>:8787
```
</details>

## How setup works without editing files

Helm mounts your server's `/` **read-only**. That lets it find drives, folders, the Tailscale socket and Proxy Manager's logs on its own. The socket proxy lets it spot companion apps like What's Up Docker and AdGuard.

Settings are saved in Helm's data volume and take effect immediately.

Two things still need a line in `docker-compose.override.yml` (git-ignored), because a container can't change its own mounts:

- making file-manager folders **editable**: the wizard prints the snippet;
- binding to a single IP.

See `docker-compose.override.example.yml`.

Environment variables in `.env` still work as defaults; see `.env.example`. Set `JARVIS_PASSWORD` there to use a fixed password instead of the one created in the browser.

## Security model

- Helm never opens the Docker socket itself. `tecnativa/docker-socket-proxy` allows only container list, inspect, stats, logs and start/stop/restart/pause, plus info. It has no exec, image, network or volume access, and publishes no ports.
- The host's `/` is mounted **read-only**. The only writable paths are folders you explicitly mount read-write in the override file.
- First-run setup needs the one-time code from the container log, so someone else on your network can't claim the dashboard before you do. Passwords are stored as salted PBKDF2 hashes.
- File operations are confined to `FILE_ROOTS`, with realpath checks: anything outside a root is refused, and roots themselves can't be deleted.
- The app runs as a non-root user. Every data endpoint needs the login cookie when a password is set.
- Helm is meant to stay on your LAN or tailnet. Don't expose it to the internet.
- The assistant sends your server snapshot only to the `ASSISTANT_URL` you configure.

## Layout

```
install.sh           one-line installer / updater
app/
  main.py            API routes + auth gate
  docker_backend.py  containers via the socket proxy, background sampler, WUD, safe updates
  hostinfo.py        CPU/memory, host network, Tailscale, AdGuard
  alerts.py          Needs-attention feed, silencing, ntfy pushes
  access.py          NPM "last accessed" + service links
  assistant.py       chat endpoint (local model or preview)
  settings.py        browser-editable settings (settings.json, env fallback)
  setup.py           first-run wizard: auto-detection + connection tests
  files.py           confined file operations
  layout.py, auth.py
  static/index.html  the whole UI
```
