# Codex Memory Maintenance

A native, silent watcher for memory-triggered **normal Quit and relaunch**. It is not a Codex automation and does not poll an LLM. The installer defaults to disabled and prepares an unloaded LaunchAgent template. No live quit/relaunch trial has been performed.

**Current installed status at 2026-11-06 02:12:11 UTC:** the root task activated the reviewed early-Accessibility gate from source commit `1b32ed1545873418ea89202f4c8dacc29abbfe70`, retaining the earlier parked-terminal correction. The signed installed executable SHA-256 is `aaa2ba7c698a3bf73e47b5089ee4719d5ab3dd405352fbfdeafb354f1940d4d6`. The same bundle path/identity, live six-hour policy, private config/state/history and LaunchAgent were preserved; the previous signed bundle is backed up. Accessibility remains ungranted. The first automatic pass and the next 60-second pass at 02:13:11 UTC both exited 0 and recorded only `accessibility_permission_required`, taking the new early-return path before monitor-history and descendant/vendor scans. The activation dry run retained its active-external-work veto. Pending restart recovery still precedes this shortcut, and explicit dry runs retain full diagnostics. No permission prompt or Quit/relaunch attempt occurred, and no memory or I/O savings have been measured. Evidence: `work/codex-idle-overhead-stage/activation-receipt.json` in the memory-audit workspace.

**Historical activation at 2026-11-05 19:22:37 UTC:** the root task enabled the private policy and loaded the LaunchAgent after source review. The final installed executable SHA-256 is `fa3b41cfe9e2e9fbd05421795b3cd882ea836ccf60b51c11b204cbeb217ed8c3`. A one-time Accessibility request was made; permission was still pending. The app-age and active-tool gates also deferred maintenance. This source-only status update does not rebuild or alter the installed helper. The local activation receipt is `~/.config/codex-memory-maintenance/activation.json`.

**Live policy update at 2026-11-06 00:31:15 UTC:** the minimum app uptime is now six hours, while the successful-restart cooldown remains 24 hours. This empirical trial allows the first idle cleanup before a full day has elapsed; the 4 GiB footprint, 15-minute sustained-pressure and user-idle requirements, activity checks, normal-Quit guard and recovery behavior are unchanged. Only that private configuration key changed; the executable was not rebuilt. The dry run was eligible on memory/age grounds and correctly vetoed active external work. Accessibility was still ungranted, and no restart occurred. Evidence: `work/rollout/liveness-runs/20261106T001419Z/codex-six-hour-minimum.json` in the memory-audit workspace.

## Policy

The prepared LaunchAgent runs every 60 seconds. New maintenance requires all of:

- Complete host-memory samples covering 15 minutes of warning/critical pressure and at least 4096 MiB direct Codex footprint.
- One unchanged app PID/version during that window; app age at least the configured minimum: six hours on this server, with a 24-hour installer default.
- At least 15 minutes without user input; at least 24 hours since the last successful or recognized replacement.
- Fresh samples and gaps no greater than 150 seconds, matching boot identity, wall time, and explicit `CLOCK_MONOTONIC_RAW` timestamps. Legacy/partial records block the window.
- A complete, unchanged descendant census, with no unknown process or identifiable external work command.

The footprint is the collector's direct app-family physical footprint, including compressed charges, not RSS or all descendant memory. The native watcher uses the same raw monotonic clock as the shared collector. It reads `~/.config/home-memory-monitor/latest.json` and bounded daily history; it makes no network requests.

For unattended `--run`, config/state validation and any already-owned restart recovery run first. If Accessibility is unavailable and no restart is pending, the helper records a silent permission deferral before loading monitor history or inspecting descendants/vendor signatures. This avoids repeated work when a new Quit is impossible. Explicit `--dry-run` retains full diagnostics. Pending relaunch recovery remains available without Accessibility, including when new maintenance is disabled. After a long permission gap, the existing terminal continuity rules require fresh observations; an unobserved interval does not count as quiet time.

## Known hosts and terminal qualification

The census recognizes the exact app-server command shape, the signed argument-free code-mode host under that app-server, the app's remote exec host, signed Electron infrastructure, bundled `node_repl`, exact Codex app-tools and CUA plugin launch shapes, the signed computer-use service, and the exact installed artifact daemon layout. User-owned plugin/runtime scripts and parent directories must be regular, not symlinks, and not writable by others. Arbitrary Node programs are blocked.

The app's persistent temporary kernel/trusted-worker scripts are allowed only when their exact bytes occur in the current signed bundled `node_repl` binary, with the exact expected arguments and parent chain under a qualified CUA host. Nested app-servers require the exact signed binary and `app-server --listen stdio://` invocation. Comparison is bounded and streamed; it does not load a binary into RAM. All descendants are still inspected, so external commands launched by these hosts veto maintenance.

A direct `/bin/zsh` with only a plain interactive/login invocation can qualify if it has a foreground controlling TTY, has a complete stable thread census in `TH_STATE_WAITING`, has no descendants, and has unchanged CPU counters and PID/start identity across a full 15-minute sequence of checks. Any command argument, script, child, CPU increase, sample gap, or clock/boot change resets or blocks this qualification. Restarting can discard these parked terminal sessions and shell state. Sleeping is not proof that a shell is displaying a prompt; waiting shell builtins remain a best-effort limitation.

## Normal quit, identity, and recovery

The target path, bundle ID, executable, Apple signature anchor, and OpenAI signing team are fixed. Valid official signed updates are allowed; version, CDHash and package SHA from installation are diagnostic baseline data, not permanent update vetoes. The current version must remain stable across the eligibility window. Resource/package identity and the signed app are revalidated at action and launch boundaries. Unknown new helper command shapes still defer safely.

The helper presses the standard **Quit ChatGPT** menu item. It retains the app's native active-local-chat/pending-worktree guard. It accepts only the exact English automation-only warning with title `Quit ChatGPT?` and exactly Quit/Cancel buttons:

> Scheduled tasks won't run while ChatGPT is closed

A recognized quit warning mentioning active chats, pending worktrees, unknown detail, changed input, changed processes, or changed eligibility is cancelled. Preexisting dialogs block a new attempt. Unknown/localized/foreign UI is never accepted or dismissed as our approval. No signals, force kills, hidden IPC, backend restart, app patch, or quit-confirmation disabling are used.

Before requesting Quit, the helper locks concurrent execution and durably records the old PID/start/boot identity. State writes fsync both the file and containing directory. AX timeouts are ambiguous and do not erase this receipt. A confirmed cancelled dialog is cleared only after disappearance. If the helper itself dies while its recognized quit dialog remains open, a later invocation cancels it, verifies disappearance and the original identity, then clears the receipt. Unknown UI remains untouched.

Normal exit has a 30-second observation bound. If it finishes later, a future invocation recognizes the original process is gone and restores the app. A manual/already completed replacement is recognized without launching a duplicate and starts the 24-hour cooldown. A missing app gets at most three launch attempts spaced five minutes apart, then at most one per hour until restored. Each attempt revalidates the signed same-path app and persists intent before launching. No duplicate instance or force kill is used. **Disabling new maintenance still permits recovery of an already-owned quit.** Boot/identity uncertainty, corrupt receipts, and unknown UI defer rather than guess.

## Safety contract and limits

This is **best-effort normal-quit maintenance**, not a guaranteed all-work idle detector. The app has no supported transactional desktop-wide idle/drain API. Its native guard tracks renderer-reported local chat activity and pending worktrees; it is not an authoritative census of remote work, background terminals, asynchronous work inside known hosts, or scheduled jobs. Vendor-origin proof is not an idle proof. The process veto and quiet window reduce risk, and the app's own guard remains the accepted activity authority.

A race remains between the last check and normal quit, especially when no warning is shown. Unknown helpers/UI safely defer; serious identity or UI failures can still require investigation in the local state. Nothing sends a notification or assumes success because a PID is old or CPU usage is low. Compiled fixtures do not establish that live Accessibility menu/dialog handling works; only an observed live attempt can qualify that path, and none has occurred.

## Build and one-time permission

`bash build-install.sh --build-only` compiles with warnings as errors and runs 86 policy/recovery/dialog/process checks,19 terminal-thread checks,14 vendor-origin checks and17 actual-entrypoint routing checks. The vendor checks safely inspect current signed binaries and temporary vendor script bytes; they never execute those scripts or an AX action. The entrypoint fixtures use inert collaborators to verify permission deferral, pending launch/quit recovery without AX, disabled-but-pending recovery and complete dry-run diagnostics without touching production files. `--install` prepares a fresh disabled installation and refuses to overwrite an existing one. It never loads launchd, requests TCC permission, or quits Codex.

Installed files are:

- `~/Applications/Codex Memory Maintenance.app`
- `~/.config/codex-memory-maintenance/config.json` and `state.json` (0600, directory 0700)
- Bounded `history.json` (256 entries) and a prepared `com.zack.codex-memory-maintenance.plist` template in that private directory.

After final review, the one-time macOS prerequisite is to enable **Codex Memory Maintenance** in System Settings → Privacy & Security → Accessibility, adding the exact installed app if needed. This permission lets the helper operate the normal Quit menu and cancel or accept its narrowly classified warning. It is never requested in a polling loop. An explicit opt-in command is available:

```sh
"$HOME/Applications/Codex Memory Maintenance.app/Contents/MacOS/CodexMemoryMaintenance" --request-accessibility
```

`--check-permissions` does not prompt. `--dry-run` reports the disabled state, the decision if enabled, and safe process classifications; it performs no quit, launch, dialog action, or state update. The execution lock may be created. Rebuilding the ad-hoc signed helper may require granting permission again; preserve the final reviewed binary.

## Activation and rollback

After review and permission, the operator may atomically set `enabled: true` in the private config, preserving ownership/mode, copy the prepared plist into `~/Library/LaunchAgents/`, and bootstrap that exact agent. The installer does not activate it. The default thresholds are conservative trial values, not proof of a Codex memory leak.

To disable new maintenance, set `enabled: false`; leave the agent running if it owns an unresolved quit so it can restore the app. To stop all watcher activity, boot out only `gui/$(id -u)/com.zack.codex-memory-maintenance`, first confirming no pending quit needs recovery. Never delete a pending receipt to make it retry blindly. State/history expose silent deferral, recovery, and failure reasons without full arguments, environment variables, or chat content.

Research and source evidence: `/Users/zack/Documents/Codex/2026-11-03/can-yo/work/codex-restart-policy-research.md`. Official guidance recommends finishing active chats before restart; local automations need the app running: [troubleshooting](https://learn.chatgpt.com/docs/reference/troubleshooting), [automations](https://learn.chatgpt.com/docs/automations), [app-server API](https://learn.chatgpt.com/docs/app-server). The descendant enumeration uses [Apple's libproc implementation](https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.c).

## Installed parked-terminal correction, November6

A read-only check found that the two idle integrated shells report BSD process status2 (`SRUN`) even while their single Mach thread is waiting, with no runnable threads. Requiring `proc_bsdinfo.pbi_status == SSLEEP` therefore vetoed them indefinitely. The installed correction reads `PROC_PIDLISTTHREADS` and each `PROC_PIDTHREADINFO.pth_run_state`, requiring every thread to be `TH_STATE_WAITING`. Thread list/count, zero runnable count, process start identity and CPU counters must remain unchanged across the bounded read; incomplete/unknown, stopped, uninterruptible or running states still veto. Exact plain-shell arguments, direct parent, foreground TTY, no children and the existing15-minute unchanged-CPU history remain required. This is a wait-state correction, not permission to interrupt shell commands.

This follows [Apple ps thread-state accounting](https://github.com/apple-oss-distributions/adv_cmds/blob/main/ps/tasks.c#L29-L45), the [XNU libproc dispatch](https://github.com/apple-oss-distributions/xnu/blob/f6217f891ac0bb64f3d375211650a4c1ff8ca1ea/bsd/kern/proc_info.c#L2310-L2334), and the installed macOS SDK's `proc_threadinfo`/`TH_STATE_WAITING` definitions. The119 source/fixture checks passed before activation. No app quit or Accessibility prompt was performed during the read-only proof or the root-owned upgrade. This fixes an indefinite false veto; it does not relax the active-work guard or establish that live AX Quit/relaunch succeeds.
