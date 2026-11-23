#!/bin/sh

# Full-decode audit for every Agregarr trailer. Pass --repair only while
# Agregarr and trailer-processor are stopped to delete failed media and stale
# processing state; Agregarr will regenerate the missing placeholders.

MODE=${1:-audit}
case "$MODE" in
    audit|--repair) ;;
    *) echo "Usage: $0 [--repair]" >&2; exit 2 ;;
esac

REPORT=$(mktemp /tmp/trailer-audit.XXXXXX) || exit 1
trap 'rm -f "$REPORT"' EXIT HUP INT TERM

decode_with_retries() {
    file=$1
    attempt=1
    while [ "$attempt" -le 3 ]; do
        if ffmpeg -nostdin -v error -xerror -i "$file" \
            -map 0:v:0 -map 0:a:0 -f null - >/dev/null 2>&1; then
            return 0
        fi
        attempt=$((attempt + 1))
        sleep 1
    done
    return 1
}

for root in /data/movies /data/tv; do
    [ -d "$root" ] || continue
    find "$root" -type f -name '*Trailer*.mp4' | while IFS= read -r file; do
        if decode_with_retries "$file"; then
            printf 'valid\t%s\n' "$file" >> "$REPORT"
        else
            printf 'invalid\t%s\n' "$file" >> "$REPORT"
            if [ "$MODE" = --repair ]; then
                rm -f "$file" "${file}.processed" "${file}.retry" \
                    "${file}.redownloads" "${file}.failed"
            fi
        fi
    done

    # State files are useful only while their exact trailer exists.
    find "$root" -type f \( -name '*Trailer*.mp4.processed' \
        -o -name '*Trailer*.mp4.retry' -o -name '*Trailer*.mp4.redownloads' \
        -o -name '*Trailer*.mp4.failed' \) | while IFS= read -r state; do
        case "$state" in
            *.processed) trailer=${state%.processed} ;;
            *.retry) trailer=${state%.retry} ;;
            *.redownloads) trailer=${state%.redownloads} ;;
            *.failed) trailer=${state%.failed} ;;
        esac
        if [ ! -f "$trailer" ]; then
            printf 'orphan-state\t%s\n' "$state" >> "$REPORT"
            [ "$MODE" = --repair ] && rm -f "$state"
        fi
    done
done

valid=$(awk -F '\t' '$1 == "valid" {count++} END {print count+0}' "$REPORT")
invalid=$(awk -F '\t' '$1 == "invalid" {count++} END {print count+0}' "$REPORT")
orphan_state=$(awk -F '\t' '$1 == "orphan-state" {count++} END {print count+0}' "$REPORT")
printf 'Trailer audit: valid=%s invalid=%s orphan_state=%s mode=%s\n' \
    "$valid" "$invalid" "$orphan_state" "$MODE"

if [ "$MODE" = audit ] && [ "$invalid" -gt 0 ]; then
    exit 1
fi
