# Native memory history in the existing Netdata agent

This read-only collector samples approximately every 60–66 seconds through the
existing `push-zfs-stats.sh` schedule and publishes gauges to the existing
Netdata StatsD listener on localhost:8125. It creates no new LaunchAgent or
persistent daemon, does not poll an LLM, does not notify, and cannot restart
applications or Docker. Canonical code is this directory. The private state
directory is `~/.config/home-memory-monitor`.

## Why this collector exists

Netdata 2.7.1's macOS source uses a byte count where `host_statistics64` expects
a count of integer units. The faulty call is executed only when at least one
of `system ram`, `swap i/o`, or `memory page faults` is enabled. The temporary
configuration disables exactly those three charts. It preserves CPU collection
and avoids the unsafe call rather than swallowing a crash. The permanent
upstream correction changes the count, but rebuilding Homebrew and its
dependencies is outside this narrow repair.

Sources: [v2.7.1 collector source](https://github.com/netdata/netdata/blob/v2.7.1/src/collectors/macos.plugin/macos_mach_smi.c),
[official fix #22553](https://github.com/netdata/netdata/pull/22553),
[upstream issue and workaround #23073](https://github.com/netdata/netdata/issues/23073).

The replacement uses native `vm_stat`, allowlisted `sysctl` keys, `ps comm`, and
one `top` snapshot per interval. The existing ZFS schedule loops about every
five seconds. A cheap shell timestamp check launches a background one-shot
sampler only when due; a nonblocking flock prevents overlap, so collection
cannot hold up the existing ZFS script. Native commands have 3-second timeouts
(5 seconds for top). The StatsD sender uses a one-second socket timeout.

The first external-plugin version successfully produced 60-second charts, but
subsequent Netdata startups encountered the older macOS spawn-server backlog
bug. The v2.7.1 source uses a backlog of five; failed plugin launches are not
retried by its directory scan. The installed transport therefore uses Netdata's
internal StatsD collector and the already existing ZFS schedule, avoiding that
startup dependency without rebuilding Netdata or adding another monitoring
stack. The external-plugin symlink was removed and its owned config switch is
`no`. The code retains the external protocol mode for a future fixed Netdata
version, but it is not the deployed path.

Sources: [official macOS spawn failure report](https://github.com/netdata/netdata/issues/23374),
[upstream spawn fix #23380](https://github.com/netdata/netdata/pull/23380),
[v2.7.1 spawn-server source](https://github.com/netdata/netdata/blob/v2.7.1/src/libnetdata/spawn_server/spawn_server_nofork.c),
[v2.7.1 plugin supervisor source](https://github.com/netdata/netdata/blob/v2.7.1/src/plugins.d/plugins_d.c),
[Netdata StatsD](https://learn.netdata.cloud/docs/collecting-metrics/statsd).

Live StatsD chart IDs begin `statsd_home.memory.`. The original disabled native
charts and the brief external-plugin charts retain their historical data.
Metrics include Netdata's supported `units`, `family`, and `name` tags. Units:

| Metric family | Units |
| --- | --- |
| host_memory, compressor, swap, codex_footprint | MiB |
| swap_io, paging | MiB/s interval average |
| faults | translation faults/s |
| pressure.level | 1 normal, 2 warning, 4 critical |
| codex_processes | processes |
| codex_uptime, collector_cost | seconds |
| collector_quality.complete | 1 complete, 0 partial |

RAM categories overlap and must not be added. Compressor physical storage
and logical compressed content are separate quantities. Missing metrics are
not sent as zero. StatsD retains the last gauge until updated, so consumers
must use producer timestamps/quality in private JSON to detect stale samples;
a continuously advancing StatsD chart alone does not prove fresh collection.
UDP has no delivery acknowledgement; installation verification compares live
producer values with Netdata's actual chart data.

## Private schema 1

`latest.json` is replaced atomically, mode 0600. Each
`history-YYYYMMDD.jsonl` contains the same compact record shape, one per sample;
dates are UTC. Retention is at most 14 UTC calendar dates and 32 MiB total.
Oldest owned history files are removed first; normal operation retains much
more than the last 30 minutes. An unusually dense current-day file is also
trimmed to complete recent records, so repeated restarts cannot defeat the
size bound. The state directory must be owned by the collector user and mode
0700; writable history and lock files must be owned regular mode-0600 files,
and symlinks are rejected. Latest is published only after history succeeds.
No credentials, process arguments, environment
variables, task content, or application database content is collected.

- `schema_version`: `1`.
- `sampled_at_epoch` and `sampled_epoch`: identical UTC epoch seconds; `sampled_at`: ISO timestamp.
- `monotonic_clock`: exactly `CLOCK_MONOTONIC_RAW`; `monotonic_seconds`: the shared kernel clock sample start; `sample_gap_seconds`: gap from the previous sample.
- `boot_time`: epoch seconds from `kern.boottime`.
- `quality`: `ok` or `partial`; `errors`: bounded diagnostic codes, no raw command output.
- `host`: `pressure_level` (1 normal, 2 warning, 4 critical), `ram_mib`,
  `compressor_mib` (physical compressor storage), `compressed_logical_mib`,
  `wired_mib`, `swap_used_mib`, `swap_in_mib_s`, `swap_out_mib_s`,
  `page_in_mib_s`, `page_out_mib_s`, `page_faults_s`, and named memory gauges.
- `codex`: `footprint_mib`, `process_count`, `matched_process_count`, `main_pid`,
  `uptime_seconds`, `main_path`, `bundle_version`, `bundle_build`.
- `source_counters`: native page size and the cumulative counters used for rates.
- `collector`: elapsed seconds, CPU seconds including child commands, and Python peak RSS MiB.

Codex includes processes whose executable is inside a recognized app bundle
whose on-disk identifier is `com.openai.codex`. This includes its bundled
renderers and services, but excludes descendant executables outside the app.
Its summed `top MEM` footprint is distinct from resident RSS and includes
compressed accounting charges; do not add it to the physical compressor size.
The bundled version/build are read from disk, not a claim about hot-updated
code in a surviving process. No CPU or process-count observation proves that
Codex is idle. A restart consumer needs its own app-wide readiness guard.

Any incomplete process match makes Codex footprint null. Missing/ambiguous
main process, collection failures, unknown pressure, or discontinuous counters
make quality partial. The first sample has null rates and partial quality;
the next complete continuous sample can be ok. Independent scheduled runs
load the last private record as a counter baseline. Gaps outside 30–150 seconds,
boot changes, clock-type changes, and counter resets are rejected. The shared
clock is explicit because macOS Python 3.9 `time.monotonic()` has a per-process
origin; that behavior changed in Python 3.10. See the
[Python clock documentation](https://docs.python.org/3/library/time.html#time.monotonic).
Consumers must reject stale, partial, legacy-clock, or null required data.
Quality `ok` means complete data, not normal memory pressure or an idle app.

## Installation and rollback

The installation appends a clearly marked `home-memory-monitor` block to the
existing `~/.homebrew/etc/netdata/netdata.conf`; the separate storage-retention
managed block and all Netdata database files remain intact. It inserts the
exact `statsd-hook.zsh` block after the shebang in `~/bin/push-zfs-stats.sh`,
preserving the rest of that script byte-for-byte. The existing loaded ZFS
LaunchAgent reads the script each loop, so no additional service or schedule
is needed. The hook runs even if the pool's later metrics check returns early.

The private installation receipt records the prior Netdata configuration and
ZFS script backups (mode 0600), hashes, transport, and owned block. The Netdata
LaunchAgent was restarted normally to activate the crash guard. No Homebrew
upgrade/build, database deletion, cloud-credential change, or Docker action
was performed.

For rollback, remove only the marked StatsD hook from `push-zfs-stats.sh`;
the original ZFS metrics remain active. **Keep the three macOS chart switches
disabled while using the affected Netdata binary.** Re-enabling them restores
the known crash. A later admitted Netdata upgrade should be tested before
removing that workaround. Never overwrite later storage changes with the
whole old config or delete Netdata databases/private history during rollback.

## Checks

```sh
/usr/bin/python3 -m unittest discover -s scripts/home-memory-monitor -p 'test_*.py'
/usr/bin/python3 scripts/home-memory-monitor/home-memory.plugin --once
```

`--once` is read-only and emits one JSON sample without writing state or
starting a service. The deployed `60 --statsd-once` path takes a nonblocking lock, returns
immediately if another run owns it or the next sample is not yet due, sends
numeric gauges, and saves private history. It has no dependencies beyond the
macOS Python standard library and creates no synthetic memory pressure.
