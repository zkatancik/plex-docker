#!/bin/sh

# Watch Agregarr placeholder roots and pass every trailer through the validator.
# The validator's content-aware marker makes repeated scans inexpensive.

MOVIES_DIR="/data/movies"
TV_DIR="/data/tv"
POSTPROCESSOR="/app/config/trailer-postprocessor.sh"
PROBE_SOURCE=
PROBE_TARGET=

cleanup_probe() {
    [ -n "$PROBE_SOURCE" ] && rm -f "$PROBE_SOURCE"
    [ -n "$PROBE_TARGET" ] && rm -f "$PROBE_TARGET"
}

trap cleanup_probe EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

verify_storage_integrity() {
    probe_dir=
    for candidate in "$MOVIES_DIR" "$TV_DIR"; do
        if [ -d "$candidate" ] && [ -w "$candidate" ]; then
            probe_dir=$candidate
            break
        fi
    done
    if [ -z "$probe_dir" ]; then
        echo "Error: no writable trailer root is available for the storage probe" >&2
        return 1
    fi

    PROBE_SOURCE=$(mktemp /tmp/trailer-storage-probe.XXXXXX) || return 1
    PROBE_TARGET="$probe_dir/.trailer-storage-probe.$$"
    # The original Docker VMM defect began immediately after the ZFS 128 KiB
    # record boundary, so test a full MiB on every watcher start.
    dd if=/dev/urandom of="$PROBE_SOURCE" bs=1024 count=1024 >/dev/null 2>&1 || return 1
    cp "$PROBE_SOURCE" "$PROBE_TARGET" || return 1
    sync "$PROBE_TARGET" 2>/dev/null || sync
    if ! cmp -s "$PROBE_SOURCE" "$PROBE_TARGET"; then
        echo "FATAL: trailer storage changed bytes during a container bind-mount write; processing is disabled" >&2
        return 1
    fi
    cleanup_probe
    PROBE_SOURCE=
    PROBE_TARGET=
    echo "Trailer storage integrity probe passed (1 MiB checksum comparison)."
}

process_file() {
    file=$1
    [ -f "$file" ] || return 0
    case "$file" in
        *Trailer*.mp4)
            sh "$POSTPROCESSOR" "$file"
            ;;
    esac
}

process_directory() {
    dir=$1
    [ -d "$dir" ] || return 0
    find "$dir" -type f -name '*Trailer*.mp4' ! -name '*.tmp' | while IFS= read -r file; do
        [ -n "$file" ] && process_file "$file"
    done
}

echo "Starting trailer validation watcher..."
if ! verify_storage_integrity; then
    exit 1
fi

process_directory "$MOVIES_DIR"
process_directory "$TV_DIR"

if command -v inotifywait >/dev/null 2>&1; then
    echo "Using inotifywait for trailer watching."
    inotifywait -r -m -e close_write,moved_to --format '%w%f' \
        "$MOVIES_DIR" "$TV_DIR" 2>/dev/null | while IFS= read -r file; do
        case "$file" in
            *Trailer*.mp4)
                sleep 1
                process_file "$file"
                ;;
        esac
    done
else
    echo "inotifywait is unavailable; scanning every 30 seconds."
    while true; do
        sleep 30
        process_directory "$MOVIES_DIR"
        process_directory "$TV_DIR"
    done
fi
