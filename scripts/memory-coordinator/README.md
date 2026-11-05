# Shared batch admission

This service coordinates participating heavy jobs. It does not execute commands, kill remote work, replace container memory limits, or control jobs that have not integrated the client. It is intentionally a single slot rather than a scheduler framework.

## Admission policy

- Bounded FIFO queue, 32 pending requests. Older live large jobs are not bypassed by smaller newcomers. A queued owner must keep polling; 30 seconds without a poll expires only its never-granted request. Its explicit queue deadline still applies.
- Submission, status reads and background ticks never grant work. Only the FIFO head's current owner poll may claim a grant. This prevents an abandoned queue entry becoming an active orphan when memory pressure later clears.
- One active lease globally, including Docker and participating native clients.
- New grants require macOS pressure level 1 and measured host swap reads **and** writes each at most 1 MiB/s for 60 continuously observed seconds. An unknown probe, counter reset, pressure increase, swap burst, or observation gap longer than 10 seconds restarts the healthy window.
- Host probes are cached for 5 seconds. Swap rates use `vm_stat` counter deltas and the actual host page size; nonzero historical swap occupancy does not block grants.
- Docker jobs additionally require guest `MemAvailable >= declared peak + 512 MiB`, sampled through a configured existing container every 15 seconds. Probe errors block Docker grants. This is a conservative admission estimate, not memory reservation; normal services can consume the available memory afterward.
- Initial maximum declared batch peak is 3,072 MiB. A declaration must describe the actual peak of the whole admitted workload. The service does not enforce that allocation; use outer Docker caps and bounded application work too.
- Native jobs share the pressure/swap gate and slot but **do not have a measured physical-headroom guarantee**. Integrate only native workloads with independently qualified peak budgets; do not interpret `guest:false` as an unrestricted host-memory permit.
- Heartbeats continue to protect an existing lease when host pressure rises. The service never frees an occupied slot merely because its owner is late, the deadline passes, or the coordinator restarts.

## Configuration and first installation

Example config contains no credential. Put real config/state/token outside repositories and restrict the containing directory to its owner:

```json
{
  "listen_host": "127.0.0.1",
  "port": 8766,
  "token_file": "/absolute/private/directory/token",
  "state_file": "/absolute/private/directory/state.json",
  "docker_path": "/usr/local/bin/docker",
  "probe_container": "nc-redis",
  "maintenance_file": "/Users/zack/.config/docker-stack-guardian/maintenance-until",
  "healthy_seconds": 60,
  "max_swap_mib_s": 1,
  "max_peak_mib": 3072,
  "guest_reserve_mib": 512,
  "max_queue": 32,
  "queued_owner_timeout_s": 30,
  "heartbeat_interval_s": 5,
  "lease_timeout_s": 30
}
```

Generate a strong token into the private token file without printing it. Mount the exact token file read-only into clients, readable only by their required user/group. Every bearer-token holder is trusted to truthfully acknowledge a stopped workload, including operator resolution. Do not expose this API to untrusted networks. Docker Desktop reachability through `host.docker.internal` must be verified before deploying integrations; the parent implementation verified loopback reachability on this host.

Initialize **once**, while no integrated jobs are running:

```sh
python3 coordinator.py --config /absolute/private/directory/config.json --initialize-state
```

That command refuses to overwrite existing state and exits without serving. Normal service command:

```sh
python3 coordinator.py --config /absolute/private/directory/config.json
```

Do not put `--initialize-state` in a service definition. Missing/corrupt state prevents normal startup. A filesystem lock prevents two coordinators using the same state path. Changes are written via atomic rename, file and directory fsync, and mode 0600. A runtime state-write failure poisons admission until restart; a later GET cannot accidentally expose an unpersisted grant. Do not delete/reset/restore old state while jobs might still be running.

No daemon or production integration is installed by these source files or tests.

Optional `maintenance_file` reuses the guardian's maintenance fence. Its contents
are a future Unix-epoch deadline in seconds. While that deadline is in the future,
new grants are blocked and the stable-health window is reset. Missing files and
expired positive deadlines release the fence; invalid, oversized or unreadable
files fail closed. The file is checked on every health read, independently of the
probe caches. Existing owners may keep heartbeating and finish safely during the
hold; maintenance does not falsely release their slot. Clearing/expiring the
fence still requires a fresh 60-second healthy observation window before new work.

## Network-isolated clients

Keep AlgoForge's `network_mode: none`. Configure the host service's optional
`bridge_dir` as `/Users/zack/.config/home-memory-coordinator/bridge`, and mount only
that directory read/write into the research worker at `/run/memory-admission-bridge`.
Use `ALGOFORGE_MEMORY_ADMISSION_URL=file:///run/memory-admission-bridge` plus exact
read-only client/token mounts. Install `bridge.py` beside `coordinator.py`.

The private parent directory remains mode 0700. The bridge directory must permit
the explicitly mounted container UID to create files (a group/ACL is preferred;
mode 0777 on this exact private directory also works across mapped Docker UIDs).
Request/response files are mode 0644 for cross-UID reading inside that exact mount.
They contain HMAC-signed JSON RPC, never the bearer token or command lines.
Both directions authenticate the nonce and payload with the shared token. Only
known API operations are dispatched through the same engine lock and durable state
as HTTP. Regular-file/no-follow checks, size limits, random filenames, bounded
directory processing and stale-response cleanup constrain the bridge. It cannot
execute commands, alter networks, or grant access to the Docker socket. Only the
trusted worker receives this mount/token; untrusted extension sandboxes must not.

The file transport needs no network route or new dependency. It adds polling
latency and can fail closed if the mounted directory becomes unavailable. Orphan
resolution and actual-work-stop requirements are identical to HTTP.

## HTTP contract

All endpoints require `Authorization: Bearer <token>`. All successful operations return HTTP 200 with JSON. Failures return JSON `{"error":"description"}` and 400 for invalid input, 401 for auth, 404 for unknown request, 409 for conflicting identity/lifecycle, 429 for queue full, or 503 for unavailable/failed durable admission. Bodies are limited to 16 KiB. No access logs, bearer values, or request bodies are logged.

`POST /v1/requests`:

```json
{
  "request_id": "unique-client-generated-UUID",
  "job": "xelp-guide-source-16",
  "peak_mib": 3072,
  "guest": true,
  "wait_timeout_s": 86400,
  "run_timeout_s": 1200
}
```

New submissions return `queued`, even when the host is healthy. Submitting the same ID and parameters is idempotent; it does not refresh queued-owner liveness. Reusing an ID for different work returns 409. IDs must be 8–80 letters/digits/hyphens/underscores; job names are at most 100 characters. Wait/run timeouts must be positive and at most 86,400 seconds. Only 128 terminal records are retained, so UUIDs must never intentionally be reused.

`GET /v1/requests/{request_id}` is an **owner poll and claim**, normally every two seconds. For queued work it refreshes liveness and may grant that exact head request. Use `/v1/status` for observational monitoring, not this endpoint. States are `queued`, `granted`, `blocked_orphan`, `released`, `cancelled`, `expired`. Responses include `request_id`, `reason`, `heartbeat_interval_s` (5), `lease_timeout_s` (30), and the original parameters. A granted response adds opaque `lease_id` and numeric Unix-epoch-seconds `run_deadline`. `run_deadline` is omitted while queued. The runtime budget starts at grant, not at the client's later work start. Existing clients that POST then poll GET remain compatible.

The queued-owner timeout defaults to 30 seconds (configurable from 5 to 300). On coordinator restart, previously queued requests get a fresh timeout grace to resume polling, but none can be granted without a current poll. Active/granted/orphan requests never expire through this queue mechanism. An already expired request cannot revive; submit a new UUID for a later attempt.

`POST /v1/requests/{id}/heartbeat` with `{"lease_id":"..."}`. Require both HTTP 200 **and** `state == "granted"`. A late owner or restarted coordinator can recover the same lease identity before its original runtime deadline. No new owner can enter during that recovery. After the runtime deadline, heartbeat returns `blocked_orphan`; the client must stop work. There is no deadline extension API.

`POST /v1/requests/{id}/release` with `{"lease_id":"...","stopped":true}` only after the actual work has finished or been stopped and joined. Release is durable before the next grant. Terminal responses are idempotent.

`POST /v1/requests/{id}/cancel` cancels queued work. Attempt this first on failed acquisition, without a GET that could claim a grant. If a lost response hid a grant, supply the lease identity and `stopped:true`; recover the exact unknown identity with a bounded cleanup GET only when the caller can prove it never started work. If the network prevents cleanup, do not start work; an unclaimed queue entry expires, while an uncertain grant holds the slot safely.

`GET /v1/status` returns active ID, FIFO IDs, health measurements, healthy-since timestamp, and nonterminal requests. It does not return bearer credentials or command lines.

## Python integration

Vendor `client.py`; no third-party packages. `Client(url, token_file).acquire(job, peak_mib, run_timeout_s, guest=True, wait_timeout_s=86400, cancel=threading.Event())` blocks until granted or raises `AdmissionError`. Start `lease.start_heartbeat()` immediately. Watch `lease.lost`, and enforce the runtime deadline. Call `lease.release(stopped=True)` only after owned work has actually ended.

There is intentionally no automatic context-manager release: cancelling an asyncio task does not stop a thread. For async integration, run acquisition in a thread with a cancellation event, wait for its cleanup, and shield/join an owned fitting thread before release. If a thread cannot stop immediately, keep the occupied/orphan lease until it really ends. Do not release in an outer cancellation handler while the fit continues.

## Supervised commands and recurring cron

```sh
python3 client.py --url http://host.docker.internal:8766 --token-file /run/secrets/memory-coordinator-token run --job nextcloud-cron --peak-mib 1024 --run-timeout 1800 --wait-timeout 86400 -- php -f /var/www/html/cron.php
```

The CLI creates an owned process group, starts heartbeats, and on timeout/heartbeat loss/cancellation sends TERM, then KILL if necessary, and waits for the entire group before releasing. A group that cannot be confirmed stopped retains its lease. Commands **must not daemonize or escape the owned process group**; use app-specific lifecycle integration for such workloads. SIGKILL of the wrapper cannot run cleanup, so the service retains an orphan instead of admitting overlapping work.

Exit status: child status on normal completion; 75 for admission expiry/unavailable; 124 for runtime/heartbeat shutdown; 130 for SIGINT/SIGTERM. Release failure prints a credential-free warning and leaves the slot for explicit resolution. A zero child status does not imply the server confirmed release; supervise the coordinator's status too.

Optional `run --status-file PATH` writes atomic, credential-free
`started_monotonic` / `elapsed_seconds` timing metadata for a local supervisor.
AlgoForge uses this to exclude admission waiting from campaign computation wall
budgets while continuing its durable database/health heartbeat. Monotonic timestamps
are comparable only within the same operating-system clock domain.

Optional `run --receipt-file PATH` adds durable cleanup retry for that command.
The existing parent directory must be owned by the command user and mode 0700;
the receipt and invocation lock are mode 0600. Use one persistent path per serial
command, outside any web-served directory. The file contains the coordinator URL,
request ID and opaque lease ID, never the bearer token or child command. The lock
is held for the entire invocation and prevents a second invocation sharing it.

The client fsyncs `not_started` before it can submit a request, then
`may_have_started` before spawning. Only after the entire owned process group is
confirmed gone does it write `stopped`. A confirmed terminal server response
removes the receipt. On the next invocation, `not_started` and `stopped` can retry
exact-request cleanup safely, including a lost grant or release response.
`may_have_started` never permits automatic cancellation: it requires an already
terminal server record or explicit operator resolution after proving the original
work stopped. Unknown/pruned records for that uncertain phase fail closed too.
Crashes between physical completion and its durable acknowledgement therefore
remain conservative, and no receipt can prove completion of escaped descendants.

For the mounted Nextcloud data directory, a suitable future opt-in is
`--receipt-file /var/ncdata/.memory-coordinator/nextcloud-cron.json`, with its parent
created mode 0700 and owned by UID 33. This path is outside `/var/www/html` and
persists container recreation. Adding the flag to the existing cron loop requires
its separately coordinated restart; installing this client alone does not enable
receipts or restart any service. Existing invocations continue without receipts.

`cron_loop.py` accepts the same URL/token/job/peak/run/wait options plus `--interval 300` and the command after `--`. It runs one admitted job at a time and waits the interval **after completion**. Queue waits cannot accumulate multiple cron children. It intentionally replaces a separate scheduler for that one command; do not run both it and the original cron scheduler simultaneously.

## Recovery and operational limits

Ordinary heartbeats update the live in-memory timestamp, while ownership,
deadline and lifecycle transitions remain durable. This avoids repeatedly
rewriting the full state history. A restart always marks active ownership as
needing recovery, regardless of the last persisted heartbeat timestamp.

Expired heartbeat/runtime or coordinator restart never automatically frees an active slot. Inspect the named job and its owning process/container first. Only after confirming it is stopped:

```sh
python3 client.py --url http://127.0.0.1:8766 --token-file /absolute/private/directory/token resolve REQUEST_ID --confirm-workload-stopped
```

This explicit acknowledgement is necessary because the host service cannot reliably prove that a job inside another process/container has stopped. A live original owner can also recover by heartbeat before its deadline. No bypass/force-grant exists. If the host remains pressured, reduce baseline pressure or complete the controlled Docker capacity change first; do not bootstrap by disabling the gate.

A five-second maintenance tick observes health and advances the same locked state
machine independently of requests. This lets clients use short queue attempts
without perpetually resetting the stable-health window. Startup still requires
60 seconds of observed health; a long observation gap resets it. Jobs wait only
up to their explicit queue deadline while polling; abandoned never-granted
requests expire after the owner timeout. A dead active owner requires operator
resolution. Consider an alert for an orphan or unusually old queue head using
existing monitoring.

Tests: `python3 -m unittest -v` (mocked health/clock and a short-lived localhost HTTP test; no real heavy jobs).
