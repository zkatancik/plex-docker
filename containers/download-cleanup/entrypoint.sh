#!/bin/sh
set -eu

: "${CLEANUP_INTERVAL_SECONDS:=21600}"
: "${RETENTION_GUARD_INTERVAL_SECONDS:=60}"
: "${QUEUE_RECONCILE_INTERVAL_SECONDS:=900}"

if [ "${1:-}" = "once" ]; then
  shift
  exec /usr/local/bin/download-cleanup "$@"
fi

guard_failures=0
next_cleanup=0
next_queue_reconcile=0
while true; do
  guard_dry_run=""
  for arg in "$@"; do
    if [ "$arg" = "--dry-run" ]; then
      guard_dry_run="--dry-run"
    fi
  done

  if /usr/local/bin/download-cleanup --guard ${guard_dry_run}; then
    guard_failures=0
  else
    guard_failures=$((guard_failures + 1))
    echo "seeding-retention guard failed; retrying after ${RETENTION_GUARD_INTERVAL_SECONDS}s" >&2
    if [ "$guard_failures" -ge 5 ]; then
      echo "seeding-retention guard failed 5 consecutive times; exiting for container restart" >&2
      exit 1
    fi
  fi

  now=$(date +%s)
  if [ "$now" -ge "$next_queue_reconcile" ]; then
    if /usr/local/bin/download-cleanup --reconcile-queue ${guard_dry_run}; then
      next_queue_reconcile=$((now + QUEUE_RECONCILE_INTERVAL_SECONDS))
    else
      echo "Arr queue reconciliation failed; retrying after ${RETENTION_GUARD_INTERVAL_SECONDS}s" >&2
    fi
  fi

  if [ "$now" -ge "$next_cleanup" ]; then
    if /usr/local/bin/download-cleanup "$@"; then
      next_cleanup=$((now + CLEANUP_INTERVAL_SECONDS))
    else
      echo "download-cleanup run failed; retrying after ${RETENTION_GUARD_INTERVAL_SECONDS}s" >&2
    fi
  fi

  sleep "$RETENTION_GUARD_INTERVAL_SECONDS"
done
