#!/bin/sh
# Scheduled PostgreSQL backups for the bundled database (docker-compose.prod.yml, profile "backup").
#
# Every BACKUP_INTERVAL_SECONDS: pg_dump -Fc to /backups, verify the archive is readable
# (pg_restore --list), keep the last BACKUP_RETENTION_DAYS days. A dump that fails verification
# is deleted and the service exits non-zero, so `restart: unless-stopped` retries and the failure
# shows in `docker compose ps` and the logs. With a managed database use its own backups instead.
#
# Backups contain document text: the volume must be on encrypted storage and copied off the host
# (docs/DEPLOYMENT.md, "Backups y restauración"). Restore is rehearsed with the procedure there.
set -eu

: "${PGHOST:=db}" "${PGUSER:?}" "${PGDATABASE:?}" "${PGPASSWORD:?}"
INTERVAL="${BACKUP_INTERVAL_SECONDS:-86400}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
DIR="${BACKUP_DIR:-/backups}"
export PGHOST PGUSER PGDATABASE PGPASSWORD

backup_once() {
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="$DIR/${PGDATABASE}-${stamp}.dump"
  partial="$target.partial"
  pg_dump -Fc --no-owner -f "$partial"
  if ! pg_restore --list "$partial" >/dev/null; then
    rm -f "$partial"
    echo "backup verification failed for $target" >&2
    return 1
  fi
  mv "$partial" "$target"
  echo "backup ok $target $(wc -c < "$target") bytes"
  find "$DIR" -name "${PGDATABASE}-*.dump" -type f -mtime +"$RETENTION_DAYS" -print -delete
}

mkdir -p "$DIR"
if [ "${1:-}" = "--once" ]; then
  backup_once
  exit $?
fi
while :; do
  backup_once || exit 1
  sleep "$INTERVAL"
done
