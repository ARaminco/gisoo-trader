#!/usr/bin/env bash
# Replace the live database with a dump from ./backups.
#
#   scripts/restore.sh trader-20260904-120000.dump          # asks for confirmation
#   scripts/restore.sh trader-20260904-120000.dump --yes    # no prompt
#
# What happens: the workers (api, engine, marketdata, scheduler) are stopped so
# nothing writes during the restore, a safety dump of the current database is
# taken (trader-<stamp>-pre-restore.dump), the dump is restored in a single
# transaction (all or nothing), then the stack is started again with
# `docker compose up -d` so the schema migration runs before the workers.
#
# Moving to another machine: copy .env and the dump, run ./start.sh once so the
# stack exists, then run this script there.
set -euo pipefail
cd "$(dirname "$0")/.."

file=${1:-}
if [ -z "$file" ]; then
  echo "usage: scripts/restore.sh <file in ./backups> [--yes]" >&2
  echo; echo "available:"; ls -1t backups/*.dump 2>/dev/null | sed 's|^backups/|  |' || echo "  (none)"
  exit 2
fi
name=$(basename "$file")
[ -f "backups/$name" ] || { echo "backups/$name not found" >&2; exit 2; }

if [ "${2:-}" != "--yes" ]; then
  echo "This REPLACES the live database ($(grep -E '^POSTGRES_DB=' .env | cut -d= -f2 || echo trader)) with backups/$name."
  echo "Open positions, settings, capital flows and history will be exactly what the dump contains."
  read -r -p "Type RESTORE to continue: " answer
  [ "$answer" = "RESTORE" ] || { echo "aborted"; exit 1; }
fi

echo "==> Stopping the workers"
docker compose stop api engine marketdata scheduler

echo "==> Restoring"
docker compose run --rm --no-deps backup restore "$name"

echo "==> Starting the stack again (migration runs first)"
docker compose up -d
docker compose ps
echo
echo "Done. Trained models are in models-<stamp>.tgz next to the dump; they are"
echo "rebuilt automatically (ML_RETRAIN_HOURS), so restoring them is optional."
