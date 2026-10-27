#!/usr/bin/env bash
# Smoke test for the Celery beat schedule against REAL Redis and PostgreSQL.
#
# Starts one worker and one beat process, waits, then checks the logs: every short-interval task
# must have been sent by beat AND have finished successfully in the worker. Long-interval tasks
# (purge 3600 s, reconcile 3600 s, reconcile-storage 86400 s) are reported as NOT observed; the
# script does not wait for them.
#
# Requirements (not provided by this script):
#   * DATABASE_URL and REDIS_URL pointing at running services, with migrations applied (alembic upgrade head)
#   * the backend dependencies installed (pip install -e ./backend[dev] or equivalent)
#
#   BEAT_SMOKE_SECONDS=75 backend/scripts/beat_smoke.sh
#
# Exit status: 0 when every short-interval task was sent and succeeded, 1 otherwise.

set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SECONDS_TO_RUN="${BEAT_SMOKE_SECONDS:-75}"
: "${DATABASE_URL:?set DATABASE_URL to a running PostgreSQL}"
: "${REDIS_URL:?set REDIS_URL to a running Redis}"

WORK_DIR="$(mktemp -d)"
trap 'kill "${WORKER_PID:-}" "${BEAT_PID:-}" 2>/dev/null || true; wait 2>/dev/null || true' EXIT

cd "$BACKEND_DIR"
CELERY=(python -m celery -A app.worker.celery_app)

"${CELERY[@]}" worker --concurrency=2 --loglevel=INFO >"$WORK_DIR/worker.log" 2>&1 &
WORKER_PID=$!
"${CELERY[@]}" beat --loglevel=INFO --schedule "$WORK_DIR/celerybeat-schedule" >"$WORK_DIR/beat.log" 2>&1 &
BEAT_PID=$!

echo "running worker and beat for ${SECONDS_TO_RUN}s (logs in $WORK_DIR)"
sleep "$SECONDS_TO_RUN"
kill "$BEAT_PID" "$WORKER_PID" 2>/dev/null || true
wait 2>/dev/null || true

SHORT_TASKS=(deliver-contextrag-events recover-stuck-jobs process-vector-deletions)
LONG_TASKS=(purge-expired-data reconcile-contextrag reconcile-storage)
failed=0

for name in "${SHORT_TASKS[@]}"; do
  # beat logs "Sending due task <name> (<task path>)"; the worker logs "Task <task path>[id] succeeded".
  task_path=$(grep -o "Sending due task ${name} ([^)]*)" "$WORK_DIR/beat.log" | head -1 | sed -E 's/.*\(([^)]*)\)/\1/' || true)
  sent=$(grep -c "Sending due task ${name} " "$WORK_DIR/beat.log" || true)
  succeeded=$(grep -c "Task ${task_path}\[.*\] succeeded" "$WORK_DIR/worker.log" || true)
  if [ "$sent" -ge 1 ] && [ "$succeeded" -ge 1 ]; then
    echo "OK    ${name}: sent ${sent}x, succeeded ${succeeded}x"
  else
    echo "FAIL  ${name}: sent ${sent}x, succeeded ${succeeded}x"
    failed=1
  fi
done

for name in "${LONG_TASKS[@]}"; do
  echo "SKIP  ${name}: interval longer than the smoke window, not observed"
done

if grep -q "Traceback" "$WORK_DIR/worker.log" "$WORK_DIR/beat.log"; then
  echo "WARN  tracebacks found in logs:"
  grep -n "Traceback" "$WORK_DIR/worker.log" "$WORK_DIR/beat.log" | head -5
  failed=1
fi

echo "logs kept at $WORK_DIR"
exit "$failed"
