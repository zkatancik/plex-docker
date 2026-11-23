#!/bin/sh
# Replaced October 8, 2026 after repeated OpenZFS kernel panics.
# No Docker, ZFS, pool, snapshot, service, or recursive shell cleanup commands.
# Alarm is a best-effort wall-clock ceiling; filesystem stalls cannot be guaranteed.
exec /usr/bin/perl -e 'alarm 20; exec @ARGV' /usr/bin/nice -n 19 \
  /Users/zack/.homebrew/opt/python@3.14/bin/python3.14 \
  /Users/zack/plex-docker/scripts/ssd-storage-maintenance.py "$@"
