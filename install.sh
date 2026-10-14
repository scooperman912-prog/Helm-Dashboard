#!/usr/bin/env bash
# Helm installer / updater.
#
#   curl -fsSL https://raw.githubusercontent.com/scooperman912-prog/helm-dashboard/main/install.sh | bash
#
# Installs into ~/helm (override with HELM_DIR=...), serves on port 8787
# (override with HELM_PORT=...). Run it again any time to update.
set -euo pipefail

REPO="https://github.com/scooperman912-prog/helm-dashboard"
DIR="${HELM_DIR:-$HOME/helm}"
PORT="${HELM_PORT:-}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n  \033[31m✕ %s\033[0m\n\n' "$*" >&2; exit 1; }

echo
bold "Helm — home server dashboard"
echo

# --- prerequisites ------------------------------------------------------------
command -v docker >/dev/null 2>&1 || die "Docker isn't installed. Install it first: https://docs.docker.com/engine/install/"
DOCKER="docker"
if ! docker info >/dev/null 2>&1; then
  if command -v sudo >/dev/null 2>&1 && sudo docker info >/dev/null 2>&1; then
    DOCKER="sudo docker"
    info "Using sudo for Docker (add yourself to the docker group to skip this)."
  else
    die "Can't talk to Docker. Is it running, and are you in the docker group?"
  fi
fi
$DOCKER compose version >/dev/null 2>&1 || die "Docker Compose v2 is required (the 'docker compose' command)."
ok "Docker is ready"

# --- get the code ---------------------------------------------------------------
if [ -d "$DIR/.git" ] && command -v git >/dev/null 2>&1; then
  info "Updating $DIR"
  git -C "$DIR" pull --ff-only --quiet || die "Couldn't update $DIR (local changes?). Try: git -C $DIR status"
elif [ -d "$DIR" ] && [ -f "$DIR/docker-compose.yml" ] && ! [ -d "$DIR/.git" ]; then
  info "Updating $DIR"
  curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 -C "$DIR"
elif command -v git >/dev/null 2>&1; then
  git clone --quiet --depth 1 "$REPO.git" "$DIR"
else
  mkdir -p "$DIR"
  curl -fsSL "$REPO/archive/refs/heads/main.tar.gz" | tar xz --strip-components=1 -C "$DIR"
fi
ok "Code is in $DIR"
cd "$DIR"

# --- .env: your user id (so created files belong to you) and the port -----------
touch .env
set_env() { grep -q "^$1=" .env || echo "$1=$2" >> .env; }
UID_FOR_FILES="$(id -u)"
[ "$UID_FOR_FILES" = "0" ] && UID_FOR_FILES="${SUDO_UID:-1000}"   # running as root/sudo: use the real user
set_env PUID "$UID_FOR_FILES"
if [ -n "$PORT" ]; then
  sed -i "/^HELM_PORT=/d" .env && echo "HELM_PORT=$PORT" >> .env
fi
PORT="$(grep '^HELM_PORT=' .env | cut -d= -f2 || true)"
PORT="${PORT:-8787}"
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT\$"; then
  if [ -z "$($DOCKER compose ps --status running -q helm 2>/dev/null)" ]; then
    die "Port $PORT is already in use. Re-run with another port, e.g.:  HELM_PORT=8788 bash install.sh"
  fi
fi

# --- build + start ----------------------------------------------------------------
info "Building and starting Helm (the first build takes a minute)…"
LOG="$(mktemp)"
if ! $DOCKER compose up -d --build --quiet-pull >"$LOG" 2>&1; then
  tail -n 15 "$LOG" | sed 's/^/    /'
  rm -f "$LOG"
  die "The build or start failed (details above)."
fi
rm -f "$LOG"
[ -n "$($DOCKER compose ps --status running -q helm 2>/dev/null)" ] || die "Helm didn't stay running. See: cd $DIR && $DOCKER compose logs helm"

for _ in $(seq 1 60); do
  curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && break
  sleep 2
done
curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 || die "Helm started but isn't answering. See: cd $DIR && $DOCKER compose logs helm"
ok "Helm is running"

# --- tell the user where to go ------------------------------------------------------
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
TS_IP="$(command -v tailscale >/dev/null 2>&1 && tailscale ip -4 2>/dev/null | head -n1 || true)"
CODE=""
if curl -fsS "http://127.0.0.1:$PORT/api/me" 2>/dev/null | grep -q '"needs_password":true'; then
  CODE="$($DOCKER compose logs helm 2>/dev/null | grep -oE '\b[A-Z0-9]{4}-[A-Z0-9]{4}\b' | tail -n1 || true)"
fi

echo
bold "Open Helm in your browser:"
[ -n "$LAN_IP" ] && info "http://$LAN_IP:$PORT"
[ -n "$TS_IP" ]  && info "http://$TS_IP:$PORT   (Tailscale)"
if [ -n "$CODE" ]; then
  echo
  bold "Your setup code:  $CODE"
  info "The page will ask for it, then walk you through the rest."
fi
echo
info "Update later:   re-run this command"
info "Logs:           cd $DIR && $DOCKER compose logs -f helm"
info "Uninstall:      cd $DIR && $DOCKER compose down -v && rm -rf $DIR"
echo
