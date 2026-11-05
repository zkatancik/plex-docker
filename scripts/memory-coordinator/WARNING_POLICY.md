# Bounded warning-pressure admission

The implementation defaults both policy switches to false. This host enabled
`warning_enabled=true` on 2026-11-05 after 70 tests and independent review;
`capacity_bypass_enabled=false` remains explicit. Runtime configuration retains
max peak 4096 MiB, guest reserve 512 MiB, and the original normal-pressure gate.
The private rollout receipt preserves code/config/state provenance. Actual job
completion is still required to qualify the added route for this workload mix.

## Reason for a conditional route

Apple distinguishes warning from critical pressure. Its pressure thresholds
depend on available non-compressed memory and have separate entry/exit
thresholds. Therefore quiet current swap does not imply pressure must already
have returned to normal. Warning is still evidence of contention, not spare
host capacity or a safe-to-allocate guarantee. These facts justify investigating
a bounded exception; they do not validate the numeric policy below.
[Apple XNU pressure documentation](https://github.com/apple-oss-distributions/xnu/blob/main/doc/vm/memorystatus_notify.md)

The sysctl returns dispatch pressure bits, not the internal enum shown in that
document: normal 1, warning 2, critical 4. The conversion merges internal
warning and urgent into the warning bit. Critical/unknown values remain a hard
deny for new work.
[Apple sysctl conversion source](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_memorystatus_notify.c)

Linux `MemAvailable` estimates memory usable for new applications without guest
swapping. It accounts for reclaimable cache/slab and operating reserves; it is
not a reservation and says nothing about macOS physical headroom. The host
pressure/swap requirements therefore remain separate.
[Linux meminfo definition](https://www.kernel.org/doc/html/latest/filesystems/proc.html#meminfo)

## Candidate behavior

The existing normal route keeps its pressure-1, swap-rate and 60-second stable
window thresholds. The warning route is opt-in and requires:

- Current pressure exactly 2, after a continuously observed five-minute quiet
  window containing only pressure 1 or 2. Safe 1/2 oscillation cannot starve
  both routes; current normal pressure still uses the normal route's own gate.
- Fresh monotonic host observations, intervals at most 10 seconds, and finite
  nonnegative measurements. The first valid observation after startup/reset
  anchors the window but contributes no preceding interval. Repeated cached
  reads cannot manufacture history. Intervals below one second are invalid;
  a hard sample-count bound also protects the small in-memory deque.
- Time-weighted rolling swap reads at most 0.5 MiB/s and writes at most
  0.1 MiB/s. Any interval over 2 MiB/s reads or 1 MiB/s writes discards the window.
- No active/invalid maintenance fence, critical/unknown pressure, missing/reset
  counters, stale readings, or unobserved gap. These clear warning readiness.
- A Docker job's declared peak plus the existing 512 MiB reserve must fit a
  fresh guest-available-memory observation (at most 20 seconds old).

The threshold numbers are empirical candidates for an observed host, not Apple
recommendations. A 4096 MiB profile is eligible when at least 4608 MiB guest
capacity is available; there is no permanent small-job-only warning limit.
Native `guest:false` work remains normal-only because this policy has no
independently qualified host physical-headroom test for it.

The warning window and probe-rate clock are injected monotonic clocks. Window
state is volatile and discarded on coordinator restart. Persisted deadlines,
queue identities, and active leases keep the existing protocol. No policy
path runs a job, kills work, revokes a running lease, or resolves an orphan.

## Capacity fairness, independently opt-in

Oldest currently fitting live work is considered first, retaining original
queue order/creation time. A younger fitting job cannot claim ahead of an
older fitting job merely by polling faster. Only the selected owner's current
GET can acquire the single global lease; submission/status/background ticks
still cannot grant.

When the queue head cannot fit the current route/capacity, at most two later
jobs may complete ahead of it. After those confirmed completions, reserve
120 seconds with no new grants for the old job to become eligible. If it fits,
its owner can claim immediately. If it still cannot fit after that bounded
pause, fitting work resumes and a new bounded cycle begins; its age is retained.
This is a count/quiet-opportunity bound, not a wall-time promise: active jobs
can run up to their declared deadlines and uncertain owners retain the slot.

Completion counts persist in queued request records. Reservation deadlines are
local monotonic state; a coordinator restart restarts the bounded hold when
needed and still requires fresh health and owner polling. Queue-owner expiry,
explicit cancellation, immutable active deadlines, and blocked-orphan recovery
retain the existing behavior.

Fairness applies to a surviving request, not a logical job that repeatedly
cancels and submits a new UUID. Fantasy's current five-second retry pattern can
lose age this way. Fixing that client lifecycle is outside this staged policy;
do not describe this candidate as an absolute no-starvation guarantee for it.
The persistent `last_capacity_deferred_at` marker preserves evidence of bounded
hold expiry even after a later reason string reports the current capacity wait.

A job whose declared peak never fits cannot be made runnable by queue policy.
Do not reduce truthful declarations to force admission. Its actual memory use
must be reduced/chunked or the capacity configuration reconsidered. Similarly,
normal-only native work cannot be guaranteed progress during permanent warning;
eligible Docker work may make bounded progress while retaining that native
waiter's age and normal-pressure opportunity.

## Review and qualification

`warning_enabled` and `capacity_bypass_enabled` are false by default. The
defaults are visible in `Coordinator.__init__`. This host activated only the
warning route under an owned maintenance fence, preserved state/request
identities, and restarted only its existing host coordinator. No token,
client/bridge, Docker configuration or container changed. The capacity-fairness
route remains disabled pending its separately owned activation window. Eight
additional offline regressions reviewed the existing bypass implementation; all
78 tests pass. This review did not change the installed coordinator source or
activate capacity bypass.

The staged tests cover the full window, mean/burst distinction, cached/stale
observations, monotonic versus wall time, actual probe completion timing,
critical/unknown/maintenance rejection, a 4096 MiB workload, native exclusion,
single-owner/orphan behavior, oldest-fitting order, two completed bypasses,
bounded reservations, and immediate old-job priority when it fits. Existing
53 contract/receipt/state/liveness tests remain in the suite. Host probes and
workloads are mocked; persistence fixtures use temporary storage.

For workload qualification, begin with
one 1536 MiB Nextcloud completion under measured quiet conditions, retain
interactive use, and compare host swap/pressure, guest availability and service
health across its full execution. Larger profiles require actual completions,
not extrapolation from that first job. Only existing ordinary jobs should supply
that qualification; no synthetic pressure or false peak declaration is appropriate.
