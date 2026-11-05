#import "policy.h"
#include <math.h>

static BOOL number(id value) {
    return [value isKindOfClass:NSNumber.class] && isfinite([value doubleValue]);
}
static BOOL validSample(NSDictionary *s) {
    if (![s isKindOfClass:NSDictionary.class] || ![s[@"schema_version"] isEqual:@1] ||
        ![s[@"monotonic_clock"] isEqual:@"CLOCK_MONOTONIC_RAW"] ||
        ![s[@"quality"] isEqual:@"ok"] || ![s[@"errors"] isKindOfClass:NSArray.class] ||
        [s[@"errors"] count] != 0) return NO;
    for (NSString *k in @[@"sampled_at_epoch", @"sampled_epoch", @"monotonic_seconds", @"boot_time"])
        if (!number(s[k])) return NO;
    if ([s[@"sampled_at_epoch"] doubleValue] != [s[@"sampled_epoch"] doubleValue]) return NO;
    NSDictionary *c = s[@"codex"], *h = s[@"host"];
    if (![c isKindOfClass:NSDictionary.class] || ![h isKindOfClass:NSDictionary.class]) return NO;
    for (NSString *k in @[@"footprint_mib", @"main_pid", @"uptime_seconds", @"process_count", @"matched_process_count"])
        if (!number(c[k])) return NO;
    if (!number(h[@"pressure_level"]) || [c[@"main_pid"] intValue] <= 1 ||
        [c[@"process_count"] intValue] < 1 ||
        [c[@"process_count"] intValue] != [c[@"matched_process_count"] intValue]) return NO;
    return YES;
}

NSString *CMMPolicy(NSDictionary *cfg, NSArray *samples, NSDictionary *latest,
                    NSDictionary *state, double now, double mono,
                    double idle, long long boot) {
    if (![cfg[@"enabled"] boolValue]) return @"disabled_pending_review";
    if (![cfg[@"schema_version"] isEqual:@1]) return @"config_schema_unknown";
    if (![state isKindOfClass:NSDictionary.class] || ![state[@"schema_version"] isEqual:@1])
        return @"state_schema_unknown";
    if (state[@"pending_restart"]) return @"previous_restart_needs_reconciliation";
    if (!validSample(latest)) return @"sample_incomplete";
    double fresh = [cfg[@"max_sample_age_seconds"] doubleValue];
    double age = now - [latest[@"sampled_at_epoch"] doubleValue];
    double monoAge = mono - [latest[@"monotonic_seconds"] doubleValue];
    if (age < -2 || monoAge < -2 || fabs(age - monoAge) > 10) return @"clock_discontinuity";
    if (age > fresh || monoAge > fresh) return @"sample_stale";
    if ([latest[@"boot_time"] longLongValue] != boot) return @"boot_changed";
    if (!isfinite(idle) || idle < [cfg[@"min_user_idle_seconds"] doubleValue]) return @"user_active";
    NSDictionary *app = latest[@"codex"];
    if (![app[@"bundle_version"] isKindOfClass:NSString.class] || ![app[@"bundle_build"] isKindOfClass:NSString.class] ||
        ![app[@"main_path"] isEqual:cfg[@"target_executable"]]) return @"app_identity_changed";
    if ([app[@"uptime_seconds"] doubleValue] < [cfg[@"min_app_uptime_seconds"] doubleValue])
        return @"app_too_young";
    if (state[@"last_success_epoch"]) {
        if (!number(state[@"last_success_epoch"])) return @"state_invalid";
        double elapsed = now - [state[@"last_success_epoch"] doubleValue];
        if (elapsed < 0) return @"clock_rollback_since_restart";
        if (elapsed < [cfg[@"cooldown_seconds"] doubleValue]) return @"restart_cooldown";
        if ([state[@"last_success_boot"] longLongValue] == boot) {
            if (![state[@"last_success_clock"] isEqual:@"CLOCK_MONOTONIC_RAW"]) return @"state_clock_unknown";
            if (!number(state[@"last_success_monotonic"])) return @"state_invalid";
            double m = mono - [state[@"last_success_monotonic"] doubleValue];
            if (m < 0 || fabs(m - elapsed) > 10) return @"clock_discontinuity_since_restart";
            if (m < [cfg[@"cooldown_seconds"] doubleValue]) return @"restart_cooldown";
        }
    }
    double window = [cfg[@"sustained_seconds"] doubleValue];
    double end = [latest[@"sampled_at_epoch"] doubleValue];
    NSMutableArray *selected = [NSMutableArray array];
    for (id s in samples) {
        if (![s isKindOfClass:NSDictionary.class] || !number(s[@"sampled_at_epoch"])) return @"history_invalid";
        double t = [s[@"sampled_at_epoch"] doubleValue];
        if (t >= end - window - fresh && t <= end) [selected addObject:s];
    }
    [selected sortUsingComparator:^NSComparisonResult(id a, id b) {
        return [a[@"sampled_at_epoch"] compare:b[@"sampled_at_epoch"]];
    }];
    // Keep one predecessor of the exact window so complete elapsed coverage is proved.
    while (selected.count > 1 && [selected[1][@"sampled_at_epoch"] doubleValue] <= end - window)
        [selected removeObjectAtIndex:0];
    if (selected.count < 2 || ![selected.lastObject isEqual:latest] ||
        end - [selected.firstObject[@"sampled_at_epoch"] doubleValue] < window)
        return @"insufficient_complete_history";
    double previous = 0, previousMono = 0;
    for (NSDictionary *s in selected) {
        if (!validSample(s)) return @"history_incomplete";
        if ([s[@"boot_time"] longLongValue] != boot) return @"history_boot_changed";
        NSDictionary *a = s[@"codex"];
        if (![a[@"main_pid"] isEqual:app[@"main_pid"]] ||
            ![a[@"bundle_build"] isEqual:app[@"bundle_build"]] ||
            ![a[@"bundle_version"] isEqual:app[@"bundle_version"]] ||
            ![a[@"main_path"] isEqual:cfg[@"target_executable"]]) return @"history_app_changed";
        int pressure = [s[@"host"][@"pressure_level"] intValue];
        if (pressure != 2 && pressure != 4) return @"pressure_not_sustained";
        if ([a[@"footprint_mib"] doubleValue] < [cfg[@"min_footprint_mib"] doubleValue])
            return @"footprint_not_sustained";
        double t = [s[@"sampled_at_epoch"] doubleValue], m = [s[@"monotonic_seconds"] doubleValue];
        if (previous && (t <= previous || m <= previousMono || t - previous > fresh ||
                         m - previousMono > fresh || fabs((t - previous) - (m - previousMono)) > 10))
            return @"history_gap_or_clock_change";
        previous = t; previousMono = m;
    }
    return @"eligible";
}

NSString *CMMDialogDecision(NSArray<NSString *> *texts, NSArray<NSString *> *buttons,
                            BOOL isNew, BOOL idle) {
    NSSet *t = [NSSet setWithArray:texts], *b = [NSSet setWithArray:buttons];
    if (!isNew || ![t containsObject:@"Quit ChatGPT?"] ||
        ![b isEqual:[NSSet setWithArray:@[@"Quit", @"Cancel"]]]) return @"unknown";
    if (!idle) return @"cancel";
    NSSet *allowed = [NSSet setWithArray:@[@"Quit ChatGPT?", @"Scheduled tasks won't run while ChatGPT is closed"]];
    return [t isEqual:allowed] ? @"accept_automations_only" : @"cancel";
}

BOOL CMMAllowedChild(NSString *path, NSString *bundle, BOOL direct) {
    NSString *relative = [path hasPrefix:[bundle stringByAppendingString:@"/"]]
        ? [path substringFromIndex:bundle.length + 1] : @"";
    if (direct && [@[@"Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                    @"Contents/Resources/native/bare-modifier-monitor"] containsObject:relative]) return YES;
    if (![relative hasPrefix:@"Contents/Frameworks/Codex Framework.framework/Versions/"]) return NO;
    NSArray *suffixes = @[@"/Helpers/Codex (Renderer).app/Contents/MacOS/Codex (Renderer)",
                         @"/Helpers/Codex (Service).app/Contents/MacOS/Codex (Service)",
                         @"/Helpers/browser_crashpad_handler"];
    for (NSString *suffix in suffixes) if ([relative hasSuffix:suffix]) return YES;
    return NO;
}

NSString *CMMProcessClass(NSString *path, NSArray *argv, NSString *cwd, NSString *bundle, NSString *home, BOOL direct) {
    if (!argv.count || ![argv[0] isEqual:path]) return @"unknown";
    NSString *resources = [bundle stringByAppendingPathComponent:@"Contents/Resources"];
    NSString *server = [resources stringByAppendingPathComponent:@"codex-cli/CodexCLI.app/Contents/MacOS/codex"];
    if ([path isEqual:server]) {
        NSUInteger command = 1;
        while (command + 1 < argv.count && [argv[command] isEqual:@"-c"]) command += 2;
        if (direct && command < argv.count && [argv[command] isEqual:@"app-server"]) {
            BOOL known = YES;
            for (NSUInteger n = command+1; n < argv.count; n++) {
                if ([argv[n] isEqual:@"--analytics-default-enabled"]) continue;
                if ([argv[n] isEqual:@"-c"] && n+1 < argv.count) { n++; continue; }
                known = NO; break;
            }
            if (known) return @"app_server";
        }
        if (direct && argv.count == 8 && [argv[1] isEqual:@"-c"] && [argv[3] isEqual:@"exec-server"] &&
            [argv[4] isEqual:@"--remote"] && [argv[6] isEqual:@"--environment-id"]) return @"remote_exec_host";
        return @"unknown";
    }
    if (CMMAllowedChild(path,bundle,direct)) return @"infrastructure";
    if ([path isEqual:[resources stringByAppendingPathComponent:@"codex-cli/bin/codex-code-mode-host"]] && argv.count == 1)
        return @"code_mode_candidate";
    if ([path isEqual:[resources stringByAppendingPathComponent:@"cua_node/bin/node_repl"]] && argv.count == 1)
        return @"bundled_node_repl";
    NSString *pluginRoot = [home stringByAppendingPathComponent:@".codex/plugins/cache/openai-bundled"];
    if ([path isEqual:[resources stringByAppendingPathComponent:@"cua_node/bin/node"]]) {
        if (argv.count == 2 && [argv[1] isEqual:@"./server.mjs"] &&
            [cwd.stringByDeletingLastPathComponent isEqual:[pluginRoot stringByAppendingPathComponent:@"codex-app-tools"]])
            return @"codex_app_tools";
        NSString *preload = argv.count > 2 ? [NSURL URLWithString:argv[2]].path : nil;
        if (argv.count == 4 && [argv[1] isEqual:@"--import"] &&
            [argv[3] isEqual:[resources stringByAppendingPathComponent:@"cua_node/lib/node_modules/@oai/cua-repl/bin/cua-repl.mjs"]] &&
            [preload.lastPathComponent isEqual:@"require-browser-access.mjs"] &&
            [preload.stringByDeletingLastPathComponent.stringByDeletingLastPathComponent isEqual:[pluginRoot stringByAppendingPathComponent:@"unified-computer-use"]])
            return @"cua_repl";
    }
    NSString *sky = [home stringByAppendingPathComponent:@".codex/computer-use/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService"];
    if (direct && [path isEqual:sky] && argv.count == 1) return @"signed_computer_use";
    NSString *artifact = [home stringByAppendingPathComponent:@"Library/Application Support/Codex/artifact-sessions/runtime-generations"];
    if (direct && argv.count == 2 && [path hasPrefix:[artifact stringByAppendingString:@"/"]]) {
        NSString *runtime = path.stringByDeletingLastPathComponent.stringByDeletingLastPathComponent;
        if ([path isEqual:[runtime stringByAppendingPathComponent:@"bin/node"]] &&
            [runtime.lastPathComponent isEqual:@"runtime"] &&
            [runtime.stringByDeletingLastPathComponent.stringByDeletingLastPathComponent isEqual:artifact] &&
            [argv[1] isEqual:[runtime stringByAppendingPathComponent:@"node_modules/@oai/artifact-tool/dist/artifact-session/host-daemon.mjs"]]) return @"artifact_host";
    }
    if (direct && [path isEqual:@"/bin/zsh"] && (argv.count == 1 ||
        (argv.count == 2 && [@[@"-l",@"-i",@"-il",@"-li",@"--login"] containsObject:argv[1]]))) return @"terminal_candidate";
    return @"unknown";
}

NSString *CMMRecoveryDecision(NSString *original, NSUInteger instances, BOOL replacementVerified,
                             NSUInteger attempts, double elapsed, NSUInteger maximum, double delay) {
    if ([original isEqual:@"same"]) return @"awaiting_original_normal_exit";
    if (![original isEqual:@"gone"]) return @"original_identity_unknown";
    if (instances > 1) return @"multiple_replacement_instances_no_action";
    if (instances == 1) return replacementVerified ? @"recognize_replacement" : @"replacement_identity_unverified";
    if (attempts && (!isfinite(elapsed) || elapsed < 0)) return @"recovery_clock_discontinuity";
    if (attempts >= maximum && elapsed < MAX(delay,3600)) return @"relaunch_hourly_retry_wait";
    if (attempts && elapsed < delay) return @"relaunch_retry_wait";
    return @"relaunch_confirmed_exit";
}

BOOL CMMUpdateTerminalQuiet(NSDictionary *snapshot, NSMutableDictionary *state, NSDictionary *cfg, double now, long long boot) {
    NSMutableDictionary *next = [NSMutableDictionary dictionary]; BOOL ready = YES;
    NSDictionary *old = [state[@"terminal_quiet"] isKindOfClass:NSDictionary.class] ? state[@"terminal_quiet"] : @{};
    for (NSDictionary *terminal in snapshot[@"terminals"]) {
        NSString *key = terminal[@"identity"]; NSDictionary *prior = old[key];
        double since = now;
        if ([prior isKindOfClass:NSDictionary.class] && [prior[@"cpu_ns"] isEqual:terminal[@"cpu_ns"]] &&
            number(prior[@"last_monotonic"]) && number(prior[@"since_monotonic"]) &&
            [prior[@"since_monotonic"] doubleValue] >= 0 && [prior[@"since_monotonic"] doubleValue] <= [prior[@"last_monotonic"] doubleValue] &&
            [prior[@"boot_time"] longLongValue] == boot && [prior[@"clock"] isEqual:@"CLOCK_MONOTONIC_RAW"] &&
            now >= [prior[@"last_monotonic"] doubleValue] && now - [prior[@"last_monotonic"] doubleValue] <= [cfg[@"max_sample_age_seconds"] doubleValue])
            since = [prior[@"since_monotonic"] doubleValue];
        next[key] = @{@"cpu_ns":terminal[@"cpu_ns"],@"since_monotonic":@(since),@"last_monotonic":@(now),@"boot_time":@(boot),@"clock":@"CLOCK_MONOTONIC_RAW"};
        if (now-since < [cfg[@"sustained_seconds"] doubleValue]) ready = NO;
    }
    state[@"terminal_quiet"] = next;
    return ready;
}
