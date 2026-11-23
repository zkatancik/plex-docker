# BEGIN home-memory-monitor StatsD hook
# Existing ZFS schedule runs every ~5s; sample native memory only once per minute.
zmodload zsh/datetime
zmodload zsh/stat
typeset -A home_memory_file_stat
home_memory_due=1
if zstat -H home_memory_file_stat -- "$HOME/.config/home-memory-monitor/latest.json" 2>/dev/null; then
    home_memory_age=$(( EPOCHSECONDS - home_memory_file_stat[mtime] ))
    if (( home_memory_age >= 0 && home_memory_age < 60 )); then
        home_memory_due=0
    fi
fi
if (( home_memory_due )); then
    # Do not delay ZFS metrics; collector's nonblocking flock rejects overlap.
    /usr/bin/python3 /Users/zack/plex-docker/scripts/home-memory-monitor/home-memory.plugin 60 --statsd-once </dev/null >/dev/null 2>&1 &!
fi
unset home_memory_due home_memory_age home_memory_file_stat
# END home-memory-monitor StatsD hook
