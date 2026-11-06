#!/bin/sh

# Validate and normalize an Agregarr trailer for direct play on Apple TV.
# A marker is trusted only when it describes the exact file we validated.

FILE=${1:-}
MAX_RETRIES=3
MARKER="${FILE}.processed"
RETRY_FILE="${FILE}.retry"
REDOWNLOAD_FILE="${FILE}.redownloads"
FAILED_FILE="${FILE}.failed"
STAGING_DIR="/app/config/temp/postprocessed"
STAGED_FILE=
DESTINATION_TMP=
MARKER_TMP=

log() {
    printf '%s\n' "$*"
}

cleanup() {
    [ -n "$STAGED_FILE" ] && rm -f "$STAGED_FILE"
    [ -n "$DESTINATION_TMP" ] && rm -f "$DESTINATION_TMP"
    [ -n "$MARKER_TMP" ] && rm -f "$MARKER_TMP"
}

trap cleanup EXIT
# Cancellation is not a bad-media result: stop before incrementing retries or
# requesting a redownload. The outer admission client joins the process group.
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

file_identity() {
    # Nanosecond mtime/ctime plus inode and size prevents an old marker from
    # applying to a newly downloaded file with the same pathname.
    stat -c '%d:%i:%s:%y:%z' "$1" 2>/dev/null
}

file_sha256() {
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

read_counter() {
    value=0
    if [ -f "$1" ]; then
        value=$(cat "$1" 2>/dev/null || printf '0')
    fi
    case "$value" in
        ''|*[!0-9]*) value=0 ;;
    esac
    printf '%s\n' "$value"
}

validate_decode() {
    # ffprobe only validates headers. Decode every video and audio packet and
    # fail on the first malformed packet, truncated atom, or missing stream.
    ffmpeg -nostdin -v error -xerror -threads 2 -filter_threads 1 -i "$1" \
        -map 0:v:0 -map 0:a:0 -f null - >/dev/null 2>&1
}

is_apple_compatible() {
    video_codec=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)
    pixel_format=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=pix_fmt -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)
    width=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=width -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)
    height=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=height -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)
    level=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=level -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)
    audio_codec=$(ffprobe -v error -select_streams a:0 \
        -show_entries stream=codec_name -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)
    channels=$(ffprobe -v error -select_streams a:0 \
        -show_entries stream=channels -of default=noprint_wrappers=1:nokey=1 \
        "$1" 2>/dev/null | head -n 1)

    case "$width:$height:$level:$channels" in
        *[!0-9:]*|*::*|:*|*:) return 1 ;;
    esac

    [ "$video_codec" = h264 ] &&
        [ "$pixel_format" = yuv420p ] &&
        [ "$audio_codec" = aac ] &&
        [ "$width" -le 3840 ] &&
        [ "$height" -le 2160 ] &&
        [ "$level" -le 52 ] &&
        [ "$channels" -le 2 ]
}

remove_for_redownload() {
    reason=$1
    redownload_count=$(read_counter "$REDOWNLOAD_FILE")
    redownload_count=$((redownload_count + 1))
    printf '%s\n' "$redownload_count" > "$REDOWNLOAD_FILE"
    log "Rejecting trailer and requesting a clean download ($reason): $FILE" >&2
    rm -f "$FILE" "$MARKER" "$RETRY_FILE" "$FAILED_FILE"
}

record_failure() {
    reason=$1
    retry_count=$(read_counter "$RETRY_FILE")
    retry_count=$((retry_count + 1))
    printf '%s\n' "$retry_count" > "$RETRY_FILE"
    log "Trailer processing failed ($reason), attempt $retry_count/$MAX_RETRIES: $FILE" >&2
    if [ "$retry_count" -ge "$MAX_RETRIES" ]; then
        remove_for_redownload "$reason after $MAX_RETRIES attempts"
        return 0
    fi
    return 1
}

if [ -z "$FILE" ] || [ ! -f "$FILE" ]; then
    log "Error: trailer file not found: $FILE" >&2
    exit 1
fi

for dependency in ffmpeg ffprobe sha256sum stat mktemp; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        log "Error: required command is unavailable: $dependency" >&2
        exit 1
    fi
done

# Old releases created empty .processed and permanent .failed markers. They do
# not prove anything about current content and are deliberately ignored.
rm -f "$FAILED_FILE"
current_identity=$(file_identity "$FILE")
marker_identity=$(sed -n 's/^identity=//p' "$MARKER" 2>/dev/null | head -n 1)
if [ -n "$current_identity" ] && [ "$marker_identity" = "$current_identity" ]; then
    exit 0
fi

# Re-enter only after admission. Checking the marker first keeps already
# validated trailers cheap, and re-entry rechecks files changed while queued.
if [ -n "${MEMORY_COORDINATOR_URL:-}" ] && [ "${TRAILER_MEMORY_SLOT_HELD:-0}" != 1 ]; then
    exec python3 "${MEMORY_COORDINATOR_CLIENT:-/opt/memory-coordinator/client.py}" \
        --url "$MEMORY_COORDINATOR_URL" \
        --token-file "${MEMORY_COORDINATOR_TOKEN_FILE:-/run/secrets/memory-coordinator-token}" \
        run --job trailer-processing --peak-mib 1024 --run-timeout 1800 --wait-timeout 86400 \
        --receipt-file /app/config/.memory-coordinator/trailer-processing.json \
        -- env TRAILER_MEMORY_SLOT_HELD=1 sh "$0" "$@"
fi

# Require two consecutive stable observations before reading or replacing the
# file. Agregarr normally moves a completed file, but this also makes polling
# safe if a future release switches to an in-place copy.
stable_count=0
previous_identity=
wait_count=0
while [ "$wait_count" -lt 30 ]; do
    observed_identity=$(file_identity "$FILE")
    if [ -n "$observed_identity" ] && [ "$observed_identity" = "$previous_identity" ] && [ -s "$FILE" ]; then
        stable_count=$((stable_count + 1))
        [ "$stable_count" -ge 2 ] && break
    else
        stable_count=0
    fi
    previous_identity=$observed_identity
    wait_count=$((wait_count + 1))
    sleep 1
done

if [ "$stable_count" -lt 2 ]; then
    record_failure 'file did not become stable'
    exit $?
fi

source_identity=$(file_identity "$FILE")
if ! validate_decode "$FILE"; then
    # A Docker bind mount can occasionally return a transient read error even
    # when the host file is intact. Require three failures on separate watcher
    # passes before replacing the source instead of deleting it immediately.
    record_failure 'full decode validation failed'
    exit $?
fi

mkdir -p "$STAGING_DIR" || exit 1
STAGED_FILE=$(mktemp "$STAGING_DIR/trailer.XXXXXX") || exit 1

if is_apple_compatible "$FILE"; then
    log "Remuxing validated H.264/AAC trailer for fast start: $FILE"
    if ! ffmpeg -nostdin -v error -xerror -threads 2 -filter_threads 1 -i "$FILE" \
        -map 0:v:0 -map 0:a:0 -sn -dn -map_metadata -1 \
        -c copy -movflags +faststart -f mp4 -y "$STAGED_FILE"; then
        record_failure 'lossless remux failed'
        exit $?
    fi
else
    source_height=$(ffprobe -v error -select_streams v:0 \
        -show_entries stream=height -of default=noprint_wrappers=1:nokey=1 \
        "$FILE" 2>/dev/null | head -n 1)
    case "$source_height" in
        ''|*[!0-9]*) source_height=1080 ;;
    esac
    if [ "$source_height" -gt 1080 ]; then
        h264_level=5.1
        maxrate=20M
        bufsize=40M
    else
        h264_level=4.1
        maxrate=8M
        bufsize=16M
    fi

    log "Transcoding validated trailer to Apple TV H.264/AAC: $FILE"
    if ! ffmpeg -nostdin -v error -xerror -threads 2 -filter_threads 1 -i "$FILE" \
        -map 0:v:0 -map 0:a:0 -sn -dn -map_metadata -1 \
        -vf "scale=w='min(3840,iw)':h='min(2160,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p" \
        -c:v libx264 -threads 2 -preset medium -crf 21 -profile:v high -level "$h264_level" \
        -maxrate "$maxrate" -bufsize "$bufsize" -g 60 \
        -c:a aac -b:a 192k -ar 48000 -ac 2 \
        -movflags +faststart -f mp4 -y "$STAGED_FILE"; then
        record_failure 'H.264/AAC transcode failed'
        exit $?
    fi
fi

if ! validate_decode "$STAGED_FILE" || ! is_apple_compatible "$STAGED_FILE"; then
    record_failure 'staged output failed compatibility or decode validation'
    exit $?
fi

# Never overwrite a newer download that arrived while this process was busy.
if [ "$(file_identity "$FILE")" != "$source_identity" ]; then
    log "Trailer changed during processing; leaving the newer file untouched: $FILE" >&2
    exit 1
fi

# Stage the final write beside the destination, then prove that the bytes which
# crossed the container bind mount are identical before the atomic rename.
DESTINATION_TMP="${FILE}.repair.$$"
if ! cp "$STAGED_FILE" "$DESTINATION_TMP"; then
    record_failure 'destination copy failed'
    exit $?
fi
sync "$DESTINATION_TMP" 2>/dev/null || sync

staged_hash=$(file_sha256 "$STAGED_FILE")
destination_hash=$(file_sha256 "$DESTINATION_TMP")
if [ -z "$staged_hash" ] || [ "$staged_hash" != "$destination_hash" ]; then
    record_failure 'destination checksum mismatch'
    exit $?
fi
if ! validate_decode "$DESTINATION_TMP" || ! is_apple_compatible "$DESTINATION_TMP"; then
    record_failure 'destination output failed compatibility or decode validation'
    exit $?
fi
if [ "$(file_identity "$FILE")" != "$source_identity" ]; then
    log "Trailer changed before final replacement; leaving the newer file untouched: $FILE" >&2
    exit 1
fi

mv "$DESTINATION_TMP" "$FILE" || exit 1
DESTINATION_TMP=
final_identity=$(file_identity "$FILE")
final_hash=$(file_sha256 "$FILE")
if [ -z "$final_identity" ] || [ -z "$final_hash" ]; then
    log "Error: could not identify final trailer: $FILE" >&2
    exit 1
fi

MARKER_TMP="${MARKER}.tmp.$$"
{
    printf 'version=2\n'
    printf 'identity=%s\n' "$final_identity"
    printf 'sha256=%s\n' "$final_hash"
} > "$MARKER_TMP" || exit 1
mv "$MARKER_TMP" "$MARKER" || exit 1
MARKER_TMP=

rm -f "$RETRY_FILE" "$REDOWNLOAD_FILE" "$FAILED_FILE"
log "Trailer is fully decoded, checksum-verified, and Apple TV compatible: $FILE"
exit 0
