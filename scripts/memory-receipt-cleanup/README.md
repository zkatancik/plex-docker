# Receipt acknowledgement cleanup — installed

Installed and enabled by the rollout owner at **2026-11-06 00:54:47 UTC**. The first actual launchd pass exited 0, completed at 00:54:48.835304 UTC in 1.328 seconds, and reported native/trailer/Algo `absent` and Nextcloud `busy_or_unsafe`. Nextcloud already held its receipt lock while waiting for admission; the controller skipped it. The maintenance fence was independently cleared at 00:55:19.467389 UTC, with 38 containers running and all defined healthchecks passing. This controller did not clear that fence.

Canonical source is `/Users/zack/plex-docker/scripts/memory-receipt-cleanup/`. The installed executable is `/Users/zack/.config/home-memory-coordinator/receipt-cleanup/cleanup.py`; independently verified SHA-256 **`488f208d943f76cfe43e25380ac4d962e050eb46ea9bf1422518473709247519`** matches this source exactly. The installed LaunchAgent is `~/Library/LaunchAgents/com.zkatancik.memory-receipt-cleanup.plist`.

The rollout's local evidence is `/Users/zack/Documents/Codex/2026-11-03/can-yo/work/rollout/liveness-runs/20261106T001419Z/fence-cleared.json`; current private controller status is `/Users/zack/.config/home-memory-coordinator/receipt-cleanup/latest.json`. Evidence is machine-local and intentionally outside this repository; no token or receipt content belongs in Git.

This native one-shot controller retries safe lifecycle acknowledgements even if no new media or research job arrives. A lost acknowledgement no longer needs the next workload invocation to trigger recovery. It runs no jobs, imports no application package, acquires no lease, and changes no coordinator policy or durable state directly.

## Exact scope

| Role | Execution identity | Existing persistent receipt | Exact original coordinator |
|---|---|---|---|
| native | zack UID501, `/usr/bin/python3` | `/Users/zack/.config/media-normalizer/memory-admission.json` | `http://127.0.0.1:8766` |
| trailer | current `trailer-processor` ID, UID0 | `/app/config/.memory-coordinator/trailer-processing.json` | `http://host.docker.internal:8766` |
| Algo | current `algoforge-worker-1` ID, UID10001 | `/app/runtime/.memory-coordinator/research.json` | `file:///run/memory-admission-bridge` |
| Nextcloud, optional | current `nc-cron` ID, UID33 | `/var/ncdata/.memory-coordinator/nextcloud-cron.json` | `http://host.docker.internal:8766` |

A bounded local context inspection requires `desktop-linux` to resolve exactly to `unix:///Users/zack/.docker/run/docker.sock`. Subsequent inspect/exec commands pin that socket with `--host`, so a concurrent context change cannot redirect them. All `DOCKER_*` and `BUILDKIT_*` ambient overrides are removed from Docker child environments. A remote, missing or mismatched context is refused. Python helpers use isolated `-I` mode. Docker lookup requests only ID/running/paused/name and executes against the captured immutable container ID. If the container stops/recreates between lookup and exec, that pass fails safely and the next pass retries. It never starts/restarts/recreates containers. The Algo file transport retains network isolation. Container helpers use `/opt/memory-coordinator/client.py` and `/run/secrets/memory-coordinator-token`; native uses the existing private host client/token files. No URL translation or token copying occurs. No token value, process arguments, environment, receipt ID, raw API body or exception message is logged.

The helper takes `RunReceipt.lock()` nonblocking, reads the private receipt, verifies its URL and reconciles **only `stopped` or `not_started`**. These phases are durable statements made by the supervised client. Busy/unsafe locks, corrupt receipts, unknown versions, wrong UID/URL and `may_have_started` are skipped intact. There is no timestamp-based inference that work died. Absent receipts create no parent or receipt lock.

Recovery reuses `RunReceipt.recover()` and `Client.cancel_unstarted()`. It sends cancellation first with the retained identity. Only an explicit HTTP409 permits that client's exact-request GET to reconcile a raced/lost grant; transport errors, authentication errors, HTTP429 and server errors issue no GET. A known `404: unknown request` is acceptable only with the durable never-started/stopped proof. Mismatched acknowledgements leave proof intact. No general status/owner polls or automatic orphan reset are performed.

## Bounds and observability

Each helper has a nine-second internal alarm and two-second per-call timeout; the maximum protocol path is cancel → conflict lookup → cancel. The outer process timeout is twelve seconds; Each Docker context and metadata lookup gets two seconds. Four roles take at most approximately 60 seconds of ordinary timeout waits, with no inner retry loop. This is a userspace timeout bound, not a guarantee against an uninterruptible kernel/filesystem operation. The Docker helper has its own alarm because killing a timed-out Docker CLI does not prove its exec process stopped. Any ambiguous failure leaves receipt proof for the next pass.

The LaunchAgent runs every 60 seconds (coalesced/delayed by launchd when appropriate), exits, and has no KeepAlive resident daemon. A separate private nonblocking controller lock prevents overlapping passes. It adds no model turn, notification or reminder. The installed plist enables optional Nextcloud; omitting `--nextcloud` limits it to the three required roles.

Only `<install>/latest.json` is persisted: at most 8 KiB, owned0600, atomic replacement and directory fsync. No history grows; stdout/stderr go to `/dev/null`. The status records per-role outcomes and elapsed time, without identities/secrets. `retry_pending` is retried on the next pass. `uncertain_skipped` requires actual owner evidence and is deliberately not force-cleared. `client_unreviewed` means an installed shared-client upgrade needs this controller's explicit compatibility review/hash update. LaunchAgent nonzero exit exposes failures before status publication. A future monitoring integration may read the small status file; this installation adds no monitoring service.

## Maintenance and reinstallation contract

1. Review `cleanup.py`, `test_cleanup.py`, and the plist; run `/usr/bin/python3 -m unittest -v test_cleanup.py` from this directory. All 19 tests use temporary private receipts and fake API/Docker operations and pass on the actual LaunchAgent interpreter, `/usr/bin/python3` 3.9.6. The real first-pass observation above is separate from these fixtures.
2. Verify shared client bytes in each already-approved mount equal `24c915e4522d8d78abb09f1eea36270664b03933590375a520aa2ede6274442d`. The controller hashes at most 128 KiB then executes those exact verified bytes in memory, avoiding a hash/import replacement race. This hash is an explicit protocol-review pin, not an assumption that future versions are unsafe; review/update it with future shared-client rollouts. Never substitute an older client with GET-before-cancel behavior.
3. For an approved reinstall, preserve the fixed destinations: `/Users/zack/.config/home-memory-coordinator/receipt-cleanup/` owned501:20 mode0700, `cleanup.py` owned501 mode0600, and `~/Library/LaunchAgents/com.zkatancik.memory-receipt-cleanup.plist` owned501 mode0600. Existing host coordinator parent is already0700. Do not create/chmod workload receipt parents from this controller: workload owners create their own private directories with their actual UID.
4. Validate the plist, bootstrap only this new LaunchAgent, then inspect its exit status and private `latest.json`. Activation can acknowledge existing safe receipts and is therefore an operational mutation; do it only within the authorized maintenance scope. It needs no container or coordinator restart, fence change or media execution. It can safely reconcile its receipt while another unrelated lease is active; server identity checks remain authoritative.
5. Disable by booting out only this LaunchAgent. Leave receipts, lock files, tokens, coordinator state and application data intact. No uninstall cleanup should clear unknown ownership.

This closes delayed ACK recovery for these durable receipts. It does not turn uncertain physical completion into proof, repair receipt-free Go clients, guarantee a memory budget fits, or make permanently unavailable infrastructure healthy. Application-level retries still own their original obligations.
