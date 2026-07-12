ARG BASE_IMAGE=python:3.12-alpine
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.source="https://github.com/zkatancik/plex-docker"
LABEL org.opencontainers.image.description="Download cleanup worker for qBittorrent and Arr hardlink libraries"

COPY scripts/download-cleanup.py /usr/local/bin/download-cleanup
COPY containers/download-cleanup/entrypoint.sh /usr/local/bin/download-cleanup-entrypoint

RUN chmod +x /usr/local/bin/download-cleanup /usr/local/bin/download-cleanup-entrypoint

ENV CLEANUP_INTERVAL_SECONDS=21600
ENV RETENTION_GUARD_INTERVAL_SECONDS=60
ENV MIN_SEED_TIME_SECONDS=2592000
ENV UNMATCHED_GRACE_SECONDS=2592000
ENV RETENTION_STATE_PATH=/state/retention-state.json

ENTRYPOINT ["/usr/local/bin/download-cleanup-entrypoint"]
