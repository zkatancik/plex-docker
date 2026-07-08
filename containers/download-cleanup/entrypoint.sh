#!/bin/sh
set -eu

: "${CLEANUP_INTERVAL_SECONDS:=21600}"

if [ "${1:-}" = "once" ]; then
  shift
  exec /usr/local/bin/download-cleanup "$@"
fi

while true; do
  if ! /usr/local/bin/download-cleanup "$@"; then
    echo "download-cleanup run failed; retrying after ${CLEANUP_INTERVAL_SECONDS}s" >&2
  fi
  sleep "$CLEANUP_INTERVAL_SECONDS"
done
