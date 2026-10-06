#!/bin/sh
# Runs INSIDE the `backup` container (the postgres image: pg_dump, pg_restore,
# busybox sh). It is bind-mounted there as /usr/local/bin/pg-backup and driven
# through docker compose; you normally use scripts/backup.sh, scripts/restore.sh,
# `.\start.ps1 -Backup` or `./start.sh backup` from the host instead.
#
#   pg-backup loop            dump now, then every BACKUP_INTERVAL_HOURS (the compose service)
#   pg-backup once            one dump, then exit      (docker compose run --rm --no-deps backup once)
#   pg-backup list            show what is in /backups
#   pg-backup restore FILE    replace the live database with /backups/FILE
#                             (stop api/engine/marketdata/scheduler first - scripts/restore.sh does)
#
# Environment (set by docker-compose.yml): PGHOST PGUSER PGPASSWORD PGDATABASE
#   BACKUP_KEEP_DAYS (14)  BACKUP_INTERVAL_HOURS (24)  BACKUP_DIR (/backups)  MODELS_DIR (/data/models)
set -u

DIR=${BACKUP_DIR:-/backups}
KEEP_DAYS=${BACKUP_KEEP_DAYS:-14}
EVERY_HOURS=${BACKUP_INTERVAL_HOURS:-24}
MODELS=${MODELS_DIR:-/data/models}
umask 077

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

wait_for_db() {
  i=0
  until pg_isready -q; do
    i=$((i + 1))
    [ "$i" -ge 60 ] && { echo "$(now) database not reachable after 60 tries" >&2; return 1; }
    sleep 5
  done
}

dump() {
  # $1 = optional suffix, e.g. "-pre-restore"
  stamp=$(date -u +%Y%m%d-%H%M%S)
  name="trader-${stamp}${1:-}.dump"
  tmp="$DIR/$name.part"
  mkdir -p "$DIR"
  if pg_dump -Fc --no-owner -f "$tmp" && mv "$tmp" "$DIR/$name"; then
    size=$(du -h "$DIR/$name" | cut -f1)
    echo "$(now) backup ok: $name ($size)"
    if [ -d "$MODELS" ]; then
      tar -czf "$DIR/models-${stamp}${1:-}.tgz" -C "$(dirname "$MODELS")" "$(basename "$MODELS")" 2>/dev/null \
        || echo "$(now) models archive skipped"
    fi
    return 0
  fi
  echo "$(now) backup FAILED" >&2
  rm -f "$tmp"
  return 1
}

prune() {
  find "$DIR" -maxdepth 1 -name 'trader-*.dump' -mtime +"$KEEP_DAYS" -print -delete | sed 's/^/  pruned: /'
  find "$DIR" -maxdepth 1 -name 'models-*.tgz' -mtime +"$KEEP_DAYS" -delete
  find "$DIR" -maxdepth 1 -name '*.part' -mmin +180 -delete
}

restore() {
  file=$1
  case "$file" in /*) path=$file ;; *) path="$DIR/$file" ;; esac
  [ -f "$path" ] || { echo "no such backup: $path" >&2; exit 2; }
  wait_for_db || exit 1
  echo "$(now) safety dump of the current database first"
  dump "-pre-restore" || exit 1
  echo "$(now) restoring $path into $PGDATABASE@$PGHOST"
  # --clean/--if-exists drop and recreate every object in the dump; one transaction = all or nothing.
  if pg_restore --clean --if-exists --no-owner --single-transaction -d "$PGDATABASE" "$path"; then
    echo "$(now) restore ok"
  else
    echo "$(now) restore FAILED - the database is unchanged (single transaction)" >&2
    exit 1
  fi
}

case "${1:-loop}" in
  once)
    wait_for_db || exit 1
    dump || exit 1
    prune
    ;;
  loop)
    echo "$(now) backup service: every ${EVERY_HOURS}h, keep ${KEEP_DAYS} days, dir $DIR"
    while true; do
      if wait_for_db; then dump; prune; fi
      sleep $((EVERY_HOURS * 3600))
    done
    ;;
  list)
    ls -lh "$DIR" 2>/dev/null || echo "(no backups yet)"
    ;;
  restore)
    [ -n "${2:-}" ] || { echo "usage: pg-backup restore <file in $DIR>" >&2; exit 2; }
    restore "$2"
    ;;
  *)
    echo "usage: pg-backup loop|once|list|restore FILE" >&2
    exit 2
    ;;
esac
