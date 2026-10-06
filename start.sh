#!/usr/bin/env bash
# Gisoo Trader - one-command startup for Linux / macOS servers.
#
#   ./start.sh                 build if needed, then start
#   ./start.sh rebuild         force a rebuild
#   ./start.sh stop            stop everything
#   ./start.sh logs            follow the logs
#   ./start.sh status          show service status
#   ./start.sh backup          take a database backup into ./backups now
#   ./start.sh restore FILE    replace the database with ./backups/FILE (see scripts/restore.sh)
#   ./start.sh autostart       install a systemd unit so it survives reboots
#
# VPS with a public hostname: put these in .env before starting -
#   COMPOSE_FILE=docker-compose.yml:docker-compose.vps.yml
#   PUBLIC_SITE_ADDRESS=trader.example.com
# Compose reads COMPOSE_FILE from .env, so every command here picks it up.

set -euo pipefail
cd "$(dirname "$0")"

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m  + %s\033[0m\n' "$1"; }
warn() { printf '\033[33m  ! %s\033[0m\n' "$1"; }
err()  { printf '\033[31m  x %s\033[0m\n' "$1"; }

command -v docker >/dev/null 2>&1 || { err "docker is not installed"; exit 1; }
docker compose version >/dev/null 2>&1 || { err "docker compose v2 is required"; exit 1; }

env_get() { grep -E "^$1=" .env 2>/dev/null | head -n1 | cut -d= -f2- || true; }

case "${1:-up}" in
  stop)    step "Stopping"; docker compose down; ok "Stopped"; exit 0 ;;
  logs)    docker compose logs -f --tail 100; exit 0 ;;
  status)  docker compose ps; exit 0 ;;
  backup)  exec scripts/backup.sh ;;
  restore) shift; exec scripts/restore.sh "$@" ;;
esac

# ---------------------------------------------------------------- .env
if [ ! -f .env ]; then
  step "Creating .env from the template"
  ( umask 077; cp .env.example .env )
  jwt=$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')
  dbpass=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i.bak "s|JWT_SECRET=change-me-to-a-long-random-string|JWT_SECRET=${jwt}|" .env
  sed -i.bak "s|POSTGRES_PASSWORD=change-this-db-password|POSTGRES_PASSWORD=${dbpass}|" .env
  panelpass=$(head -c 9 /dev/urandom | od -An -tx1 | tr -d ' \n')
  sed -i.bak "s|^PANEL_PASSWORD=change-me-now|PANEL_PASSWORD=${panelpass}|" .env
  rm -f .env.bak
  chmod 600 .env
  ok ".env created with generated secrets"
  warn "Panel login: admin / ${panelpass}  (change it in .env or set PANEL_PASSWORD_HASH)"
  warn "Fill in EXCHANGE, EXCHANGE_API_KEY / EXCHANGE_API_SECRET (not needed in paper mode) and PANEL_PASSWORD, then run this again."
  warn "Public hostname? Also set PUBLIC_SITE_ADDRESS and COMPOSE_FILE (see the header of this script)."
  exit 0
fi
chmod 600 .env 2>/dev/null || true

grep -q '^PANEL_PASSWORD=change-me-now' .env && grep -q "^PANEL_PASSWORD_HASH=''" .env && \
  warn "PANEL_PASSWORD is still the default - the panel refuses every login until you change it."

PORT=$(env_get PANEL_PORT); PORT=${PORT:-8090}
BIND=$(env_get PANEL_BIND); BIND=${BIND:-127.0.0.1}
SITE=$(env_get PUBLIC_SITE_ADDRESS); SITE=${SITE:-:80}
COMPOSE_FILES=$(env_get COMPOSE_FILE)
VPS_MODE=0
case "$SITE" in :[0-9]*) ;; *) VPS_MODE=1 ;; esac

if [ "$VPS_MODE" = 1 ] && [ -z "$COMPOSE_FILES" ]; then
  warn "PUBLIC_SITE_ADDRESS=$SITE but COMPOSE_FILE is not set in .env."
  warn "Add: COMPOSE_FILE=docker-compose.yml:docker-compose.vps.yml  (publishes 80/443 for TLS)"
fi
if [ "$VPS_MODE" = 0 ] && [ "$BIND" = "0.0.0.0" ]; then
  echo "    Panel is reachable from every network on port $PORT (PANEL_BIND=0.0.0.0); set PANEL_BIND=127.0.0.1 to keep it local."
fi

# ---------------------------------------------------------------- clock
# Binance rejects signed requests whose timestamp is off by more than a few
# seconds (error -1021); warn if the host clock drifts from the exchange.
EXCHANGE_NAME=$(env_get EXCHANGE); EXCHANGE_NAME=${EXCHANGE_NAME:-binance}
if [ "$EXCHANGE_NAME" = binance ]; then
step "Checking the clock against Binance"
fetch() {
  if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 5 "$1"
  elif command -v wget >/dev/null 2>&1; then wget -qO- --timeout=5 "$1"
  else return 1; fi
}
now_ms() {
  local t; t=$(date +%s%3N 2>/dev/null || true)
  case "$t" in *N*|"") echo $(( $(date +%s) * 1000 )) ;; *) echo "$t" ;; esac
}
if body=$(fetch https://api.binance.com/api/v3/time 2>/dev/null); then
  server=$(printf '%s' "$body" | sed -n 's/.*"serverTime":[[:space:]]*\([0-9]*\).*/\1/p')
  local_ms=$(now_ms)
  if [ -n "$server" ]; then
    offset=$(( local_ms - server ))
    abs=${offset#-}
    if [ "$abs" -gt 2000 ]; then
      warn "Local clock is $((abs / 1000)).$(( (abs % 1000) / 100 )) s $([ "$offset" -gt 0 ] && echo 'AHEAD of' || echo 'BEHIND') Binance time - signed orders will fail when this drifts."
      warn "Enable time sync:  sudo timedatectl set-ntp true && timedatectl    (or install chrony)"
    else
      ok "Clock is within $((abs / 1000)).$(( (abs % 1000) / 100 )) s of Binance"
    fi
  else
    warn "Could not parse Binance time"
  fi
else
  warn "Could not reach api.binance.com to compare the clock (no curl/wget, or offline)"
fi
fi

# ---------------------------------------------------------------- autostart
if [ "${1:-}" = "autostart" ]; then
  step "Installing the systemd unit"
  unit=/etc/systemd/system/gisoo-trader.service
  docker_bin=$(command -v docker)
  sudo tee "$unit" >/dev/null <<UNIT
[Unit]
Description=Gisoo Trader
Requires=docker.service
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$(pwd)
ExecStart=${docker_bin} compose up -d --remove-orphans
ExecStop=${docker_bin} compose down
TimeoutStartSec=0
TimeoutStopSec=180

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable gisoo-trader.service
  ok "Enabled - it will start on boot (COMPOSE_FILE from .env applies there too)"
  echo "    Also on the VPS, once:"
  echo "      sudo timedatectl set-ntp true                                  # clock"
  echo "      sudo ufw allow 22/tcp && sudo ufw allow 80,443/tcp && sudo ufw allow 443/udp && sudo ufw enable"
  echo "      /etc/docker/daemon.json: {\"log-driver\":\"json-file\",\"log-opts\":{\"max-size\":\"10m\",\"max-file\":\"5\"}}"
fi

# ---------------------------------------------------------------- build & run
mkdir -p backups

if [ "${1:-}" = "rebuild" ]; then
  step "Rebuilding images"
  docker compose build --no-cache
else
  step "Building images if needed"
  docker compose build
fi

step "Starting services (the schema migration runs first)"
docker compose up -d --remove-orphans

step "Waiting for the API"
for i in $(seq 1 60); do
  sleep 2
  id=$(docker compose ps -q api 2>/dev/null | head -n1 || true)
  if [ -n "$id" ] && [ "$(docker inspect -f '{{.State.Health.Status}}' "$id" 2>/dev/null)" = "healthy" ]; then
    if [ "$VPS_MODE" = 1 ]; then
      ok "Panel is up: https://${SITE}   (first start: Caddy needs a minute to obtain the certificate)"
    else
      ok "Panel is up: http://localhost:${PORT}"
    fi
    docker compose ps
    exit 0
  fi
  [ $((i % 5)) -eq 0 ] && echo "    still waiting... ($((i*2))s)"
done

warn "The API did not become healthy in time. Check: docker compose logs -f migrate api"
docker compose ps
