#!/usr/bin/env bash
# Take a database backup right now into ./backups (pg_dump custom format +
# the trained models). Works on Linux, macOS and Git Bash on Windows; the
# stack must be running (postgres at least).
#
#   scripts/backup.sh
#
# The `backup` service also does this by itself every BACKUP_INTERVAL_HOURS
# (default 24) and deletes files older than BACKUP_KEEP_DAYS (default 14).
# A backup on the same disk is not a backup: sync ./backups somewhere else.
set -euo pipefail
cd "$(dirname "$0")/.."

mkdir -p backups
docker compose run --rm --no-deps backup once
echo
echo "Newest backups:"
ls -lt backups | head -n 6
