FROM python:3.12-alpine

LABEL org.opencontainers.image.source="https://github.com/zkatancik/plex-docker"
LABEL org.opencontainers.image.description="Download cleanup worker for qBittorrent and Arr hardlink libraries"

COPY scripts/download-cleanup.py /usr/local/bin/download-cleanup
COPY containers/download-cleanup/entrypoint.sh /usr/local/bin/download-cleanup-entrypoint

RUN chmod +x /usr/local/bin/download-cleanup /usr/local/bin/download-cleanup-entrypoint

ENV CLEANUP_INTERVAL_SECONDS=21600

ENTRYPOINT ["/usr/local/bin/download-cleanup-entrypoint"]
