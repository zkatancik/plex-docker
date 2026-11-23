#!/bin/sh

# Watch Agregarr placeholder roots and pass every trailer through the validator.
# The validator's content-aware marker makes repeated scans inexpensive.

MOVIES_DIR="/data/movies"
TV_DIR="/data/tv"
POSTPROCESSOR="/app/config/trailer-postprocessor.sh"
PROBE_SOURCE=
PROBE_TARGET=
WORK_DIR=
EVENT_FIFO=
PRODUCER_PID=
PRODUCER_START=
PROCESSOR_PID=
PROCESSOR_START=
LAUNCHING_CHILD=
STOP_STATUS=

cleanup_probe() {
    [ -n "$PROBE_SOURCE" ] && rm -f "$PROBE_SOURCE"
    [ -n "$PROBE_TARGET" ] && rm -f "$PROBE_TARGET"
}

# Signal only children started here. The postprocessor execs the admission
# client, so this PID remains its owner while queued and while supervising work.
child_identity() {
    identity_pid=$1
    case "$identity_pid" in ''|*[!0-9]*) return 1 ;; esac
    IFS= read -r identity_stat < "/proc/$identity_pid/stat" 2>/dev/null || return 1
    # Everything after the final ") " is numeric except the one-letter state.
    # This handles spaces and parentheses in comm without reading command args.
    identity_fields=${identity_stat##*) }
    [ "$identity_fields" != "$identity_stat" ] || return 1
    set -- $identity_fields
    [ "$#" -ge 20 ] && [ "$2" = "$$" ] || return 1
    shift 19
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    printf '%s\n' "$1"
}

signal_owned_child() {
    signal_pid=$1
    signal_start=$2
    [ -n "$signal_pid" ] && [ -n "$signal_start" ] || return 0
    observed_start=$(child_identity "$signal_pid") || return 0
    [ "$observed_start" = "$signal_start" ] || return 0
    kill -TERM "$signal_pid" 2>/dev/null || :
}

stop_owned_children() {
    signal_owned_child "$PRODUCER_PID" "$PRODUCER_START"
    signal_owned_child "$PROCESSOR_PID" "$PROCESSOR_START"
    if [ -n "$PROCESSOR_PID" ]; then
        wait "$PROCESSOR_PID" 2>/dev/null || :
        PROCESSOR_PID=
        PROCESSOR_START=
    fi
    if [ -n "$PRODUCER_PID" ]; then
        wait "$PRODUCER_PID" 2>/dev/null || :
        PRODUCER_PID=
        PRODUCER_START=
    fi
}

cleanup() {
    stop_owned_children
    cleanup_probe
    [ -z "$EVENT_FIFO" ] || rm -f "$EVENT_FIFO"
    [ -z "$WORK_DIR" ] || rmdir "$WORK_DIR" 2>/dev/null || :
}

request_stop() {
    STOP_STATUS=${STOP_STATUS:-$1}
    # Defer exit across fork/$! registration, so no newly owned child escapes.
    [ "$LAUNCHING_CHILD" != 1 ] || return 0
    trap '' HUP INT TERM
    stop_owned_children
    exit "$STOP_STATUS"
}

trap cleanup EXIT
trap 'request_stop 129' HUP
trap 'request_stop 130' INT
trap 'request_stop 143' TERM

run_owned() {
    LAUNCHING_CHILD=1
    (exec "$@") &
    PROCESSOR_PID=$!
    PROCESSOR_START=$(child_identity "$PROCESSOR_PID") || PROCESSOR_START=
    LAUNCHING_CHILD=
    [ -z "$STOP_STATUS" ] || request_stop "$STOP_STATUS"
    # A builtin wait is interruptible; a foreground external command is not.
    wait "$PROCESSOR_PID"
    process_status=$?
    PROCESSOR_PID=
    PROCESSOR_START=
    return "$process_status"
}

start_producer() {
    LAUNCHING_CHILD=1
    (exec "$@" > "$EVENT_FIFO") &
    PRODUCER_PID=$!
    PRODUCER_START=$(child_identity "$PRODUCER_PID") || PRODUCER_START=
    LAUNCHING_CHILD=
    [ -z "$STOP_STATUS" ] || request_stop "$STOP_STATUS"
}

join_producer() {
    wait "$PRODUCER_PID"
    producer_status=$?
    PRODUCER_PID=
    PRODUCER_START=
    return "$producer_status"
}

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
            run_owned sh "$POSTPROCESSOR" "$file"
            ;;
    esac
}

process_directory() {
    dir=$1
    [ -d "$dir" ] || return 0
    start_producer find "$dir" -type f -name '*Trailer*.mp4' ! -name '*.tmp'
    while IFS= read -r file; do
        [ -n "$file" ] && process_file "$file"
    done < "$EVENT_FIFO"
    join_producer
}

echo "Starting trailer validation watcher..."
if ! verify_storage_integrity; then
    exit 1
fi

WORK_DIR=$(mktemp -d /tmp/trailer-watcher.XXXXXX) || exit 1
EVENT_FIFO="$WORK_DIR/events"
mkfifo "$EVENT_FIFO" || exit 1

process_directory "$MOVIES_DIR"
process_directory "$TV_DIR"

if command -v inotifywait >/dev/null 2>&1; then
    echo "Using inotifywait for trailer watching."
    start_producer inotifywait -r -m -e close_write,moved_to --format '%w%f' \
        "$MOVIES_DIR" "$TV_DIR" 2>/dev/null
    while IFS= read -r file; do
        case "$file" in
            *Trailer*.mp4)
                run_owned sleep 1
                process_file "$file"
                ;;
        esac
    done < "$EVENT_FIFO"
    join_producer
else
    echo "inotifywait is unavailable; scanning every 30 seconds."
    while true; do
        run_owned sleep 30
        process_directory "$MOVIES_DIR"
        process_directory "$TV_DIR"
    done
fi
