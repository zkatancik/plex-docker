#import <Foundation/Foundation.h>
#import "policy.h"

static int checks;
static const double now = 2000000, mono = 200000;
static const long long boot = 1800000;
static NSString *bundle = @"/Applications/ChatGPT.app";
static NSMutableDictionary *config(void) {
    return [@{@"schema_version": @1, @"enabled": @YES,
              @"target_version": @"26.1027.61615", @"target_build": @"24141",
              @"target_executable": @"/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
              @"max_sample_age_seconds": @150, @"sustained_seconds": @900,
              @"min_footprint_mib": @4096, @"min_app_uptime_seconds": @86400,
              @"min_user_idle_seconds": @900, @"cooldown_seconds": @86400} mutableCopy];
}
static NSMutableArray *samples(void) {
    NSMutableArray *out = [NSMutableArray array];
    for (int i = 0; i <= 15; i++) {
        double age = (15 - i) * 60;
        NSMutableDictionary *sample = [@{@"schema_version": @1, @"sampled_at_epoch": @(now - age),
            @"sampled_epoch": @(now - age), @"monotonic_seconds": @(mono - age), @"boot_time": @(boot),
            @"quality": @"ok", @"errors": @[], @"monotonic_clock": @"CLOCK_MONOTONIC_RAW",
            @"host": [@{@"pressure_level": @2} mutableCopy],
            @"codex": [@{@"footprint_mib": @5000, @"main_pid": @517, @"uptime_seconds": @(90000-age),
                @"process_count": @50, @"matched_process_count": @50,
                @"bundle_version": @"26.1027.61615", @"bundle_build": @"24141",
                @"main_path": @"/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"} mutableCopy]} mutableCopy];
        [out addObject:sample];
    }
    return out;
}
static void equal(NSString *name, id actual, id expected) {
    checks++;
    if (![actual isEqual:expected]) {
        fprintf(stderr, "FAIL %s: got %s, expected %s\n", name.UTF8String,
                [[actual description] UTF8String], [[expected description] UTF8String]); exit(1);
    }
}
static NSString *policy(NSDictionary *cfg, NSArray *s, NSDictionary *state) {
    return CMMPolicy(cfg, s, s.lastObject, state, now, mono, 1000, boot);
}
int main(void) { @autoreleasepool {
    NSDictionary *state = @{@"schema_version": @1};
    NSMutableDictionary *cfg = config(); NSMutableArray *s = samples();
    equal(@"complete sustained pressure qualifies", policy(cfg,s,state), @"eligible");
    cfg[@"enabled"] = @NO; equal(@"review disabled",policy(cfg,s,state),@"disabled_pending_review"); cfg = config();
    equal(@"unknown state",policy(cfg,s,@{}),@"state_schema_unknown");
    equal(@"unfinished attempt",policy(cfg,s,@{@"schema_version":@1,@"pending_restart":@{}}),@"previous_restart_needs_reconciliation");
    equal(@"stale sample",CMMPolicy(cfg,s,s.lastObject,state,now+151,mono+151,1000,boot),@"sample_stale");
    equal(@"future sample",CMMPolicy(cfg,s,s.lastObject,state,now-3,mono-3,1000,boot),@"clock_discontinuity");
    equal(@"wall clock jump",CMMPolicy(cfg,s,s.lastObject,state,now+30,mono,1000,boot),@"clock_discontinuity");
    equal(@"boot change",CMMPolicy(cfg,s,s.lastObject,state,now,mono,1000,boot+1),@"boot_changed");
    equal(@"user activity",CMMPolicy(cfg,s,s.lastObject,state,now,mono,899,boot),@"user_active");
    s.lastObject[@"quality"] = @"partial"; equal(@"partial latest",policy(cfg,s,state),@"sample_incomplete"); s=samples();
    s.lastObject[@"errors"] = @[@"rate_baseline_missing"]; equal(@"error latest",policy(cfg,s,state),@"sample_incomplete"); s=samples();
    s.lastObject[@"sampled_epoch"] = @0; equal(@"alias mismatch",policy(cfg,s,state),@"sample_incomplete"); s=samples();
    s.lastObject[@"codex"][@"matched_process_count"] = @49; equal(@"incomplete footprint",policy(cfg,s,state),@"sample_incomplete"); s=samples();
    s.lastObject[@"codex"][@"uptime_seconds"] = @86399; equal(@"young app",policy(cfg,s,state),@"app_too_young"); s=samples();
    s.lastObject[@"codex"][@"bundle_build"] = @"24142"; equal(@"version changed inside window",policy(cfg,s,state),@"history_app_changed"); s=samples();
    s[4][@"codex"][@"main_pid"] = @918; equal(@"PID changed in window",policy(cfg,s,state),@"history_app_changed"); s=samples();
    s[4][@"quality"] = @"partial"; equal(@"historical partial",policy(cfg,s,state),@"history_incomplete"); s=samples();
    s[4][@"boot_time"] = @1; equal(@"historical reboot",policy(cfg,s,state),@"history_boot_changed"); s=samples();
    s[4][@"host"][@"pressure_level"] = @1; equal(@"normal pressure breaks window",policy(cfg,s,state),@"pressure_not_sustained"); s=samples();
    s[4][@"host"][@"pressure_level"] = @4; equal(@"critical pressure qualifies",policy(cfg,s,state),@"eligible"); s=samples();
    s[4][@"host"][@"pressure_level"] = @8; equal(@"unknown pressure",policy(cfg,s,state),@"pressure_not_sustained"); s=samples();
    s[4][@"codex"][@"footprint_mib"] = @4095; equal(@"footprint must be sustained",policy(cfg,s,state),@"footprint_not_sustained"); s=samples();
    [s removeObjectsInRange:NSMakeRange(5,3)]; equal(@"history gap",policy(cfg,s,state),@"history_gap_or_clock_change"); s=samples();
    [s insertObject:s[4] atIndex:5]; equal(@"duplicate epoch",policy(cfg,s,state),@"history_gap_or_clock_change"); s=samples();
    s[4][@"monotonic_seconds"] = @1; equal(@"historical clock change",policy(cfg,s,state),@"history_gap_or_clock_change"); s=samples();
    [s removeObjectAtIndex:0]; equal(@"full duration required",policy(cfg,s,state),@"insufficient_complete_history"); s=samples();
    equal(@"new latest absent from history",CMMPolicy(cfg,@[],s.lastObject,state,now,mono,1000,boot),@"insufficient_complete_history");
    equal(@"success cooldown",policy(cfg,s,@{@"schema_version":@1,@"last_success_epoch":@(now-100)}),@"restart_cooldown");
    equal(@"future success",policy(cfg,s,@{@"schema_version":@1,@"last_success_epoch":@(now+100)}),@"clock_rollback_since_restart");
    equal(@"success monotonic disagreement",policy(cfg,s,@{@"schema_version":@1,@"last_success_epoch":@(now-90000),@"last_success_boot":@(boot),@"last_success_clock":@"CLOCK_MONOTONIC_RAW",@"last_success_monotonic":@(mono-100000)}),@"clock_discontinuity_since_restart");
    equal(@"cooldown elapsed",policy(cfg,s,@{@"schema_version":@1,@"last_success_epoch":@(now-90000),@"last_success_boot":@(boot),@"last_success_clock":@"CLOCK_MONOTONIC_RAW",@"last_success_monotonic":@(mono-90000)}),@"eligible");
    for (NSMutableDictionary *row in s) { row[@"codex"][@"bundle_build"] = @"25000"; row[@"codex"][@"bundle_version"] = @"27.0"; }
    equal(@"stable official update allowed by policy",policy(cfg,s,state),@"eligible"); s=samples();
    [s[3] removeObjectForKey:@"monotonic_clock"]; equal(@"legacy monotonic domain rejected",policy(cfg,s,state),@"history_incomplete"); s=samples();
    equal(@"original alive no relaunch",CMMRecoveryDecision(@"same",1,NO,0,0,3,300),@"awaiting_original_normal_exit");
    equal(@"unknown original no relaunch",CMMRecoveryDecision(@"unknown",0,NO,0,0,3,300),@"original_identity_unknown");
    equal(@"delayed confirmed exit recovered",CMMRecoveryDecision(@"gone",0,NO,0,0,3,300),@"relaunch_confirmed_exit");
    equal(@"manual relaunch recognized",CMMRecoveryDecision(@"gone",1,YES,99,0,3,300),@"recognize_replacement");
    equal(@"unverified replacement never duplicated",CMMRecoveryDecision(@"gone",1,NO,0,0,3,300),@"replacement_identity_unverified");
    equal(@"multiple instances never duplicated",CMMRecoveryDecision(@"gone",2,YES,0,0,3,300),@"multiple_replacement_instances_no_action");
    equal(@"launch timeout waits",CMMRecoveryDecision(@"gone",0,NO,1,299,3,300),@"relaunch_retry_wait");
    equal(@"bounded retry eligible",CMMRecoveryDecision(@"gone",0,NO,1,300,3,300),@"relaunch_confirmed_exit");
    equal(@"clock rollback blocks retry",CMMRecoveryDecision(@"gone",0,NO,1,-1,3,300),@"recovery_clock_discontinuity");
    equal(@"after burst retries hourly",CMMRecoveryDecision(@"gone",0,NO,3,3599,3,300),@"relaunch_hourly_retry_wait");
    equal(@"hourly recovery renews automatically",CMMRecoveryDecision(@"gone",0,NO,3,3600,3,300),@"relaunch_confirmed_exit");
    NSArray *text = @[@"Quit ChatGPT?",@"Scheduled tasks won't run while ChatGPT is closed"];
    NSArray *buttons = @[@"Quit",@"Cancel"];
    equal(@"exact automations warning",CMMDialogDecision(text,buttons,YES,YES),@"accept_automations_only");
    equal(@"input resumed",CMMDialogDecision(text,buttons,YES,NO),@"cancel");
    equal(@"preexisting dialog",CMMDialogDecision(text,buttons,NO,YES),@"unknown");
    equal(@"active chat warning",CMMDialogDecision(@[@"Quit ChatGPT?",@"A chat is still running"],buttons,YES,YES),@"cancel");
    equal(@"pending worktree warning",CMMDialogDecision(@[@"Quit ChatGPT?",@"Worktree setup is still running"],buttons,YES,YES),@"cancel");
    equal(@"unknown warning",CMMDialogDecision([text arrayByAddingObject:@"Additional warning"],buttons,YES,YES),@"cancel");
    equal(@"localized warning",CMMDialogDecision(@[@"Quitter ChatGPT ?",@"Tâches planifiées"],@[@"Quitter",@"Annuler"],YES,YES),@"unknown");
    equal(@"punctuation mismatch",CMMDialogDecision(@[@"Quit ChatGPT?",@"Scheduled tasks won't run while ChatGPT is closed."],buttons,YES,YES),@"cancel");
    equal(@"foreign dialog",CMMDialogDecision(@[@"Allow this tool?"],@[@"Allow",@"Cancel"],YES,YES),@"unknown");
    equal(@"extra button",CMMDialogDecision(text,@[@"Quit",@"Cancel",@"Save"],YES,YES),@"unknown");
    NSString *server = [bundle stringByAppendingPathComponent:@"Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"];
    equal(@"one direct app server allowed",@(CMMAllowedChild(server,bundle,YES)),@YES);
    equal(@"nested server is tool work",@(CMMAllowedChild(server,bundle,NO)),@NO);
    equal(@"external shell blocks",@(CMMAllowedChild(@"/bin/zsh",bundle,NO)),@NO);
    equal(@"embedded node blocks",@(CMMAllowedChild([bundle stringByAppendingPathComponent:@"Contents/Resources/node"],bundle,YES)),@NO);
    equal(@"prefix lookalike blocks",@(CMMAllowedChild([@"/Applications/ChatGPT.app.other" stringByAppendingPathComponent:@"Contents/Resources/native/bare-modifier-monitor"],bundle,YES)),@NO);
    equal(@"renderer allowed",@(CMMAllowedChild([bundle stringByAppendingPathComponent:@"Contents/Frameworks/Codex Framework.framework/Versions/A/Helpers/Codex (Renderer).app/Contents/MacOS/Codex (Renderer)"],bundle,NO)),@YES);
    NSString *home = @"/Users/test", *node = [bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/bin/node"];
    NSString *repl = [bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/bin/node_repl"];
    equal(@"exact node repl host",CMMProcessClass(repl,@[repl],@"",bundle,home,NO),@"bundled_node_repl");
    equal(@"real direct app server",CMMProcessClass(server,@[server,@"-c",@"key=value",@"app-server",@"--analytics-default-enabled",@"-c",@"other=value"],@"",bundle,home,YES),@"app_server");
    equal(@"app-server argument is not command",CMMProcessClass(server,@[server,@"exec",@"app-server"],@"",bundle,home,YES),@"unknown");
    equal(@"config app-server value is not command",CMMProcessClass(server,@[server,@"-c",@"app-server",@"exec"],@"",bundle,home,YES),@"unknown");
    equal(@"node repl extra command blocks",CMMProcessClass(repl,@[repl,@"script.js"],@"",bundle,home,NO),@"unknown");
    NSString *codeHost = [bundle stringByAppendingPathComponent:@"Contents/Resources/codex-cli/bin/codex-code-mode-host"];
    equal(@"exact code-mode candidate",CMMProcessClass(codeHost,@[codeHost],@"",bundle,home,NO),@"code_mode_candidate");
    equal(@"code-mode extra argument blocks",CMMProcessClass(codeHost,@[codeHost,@"work"],@"",bundle,home,NO),@"unknown");
    equal(@"external code-mode lookalike blocks",CMMProcessClass(@"/tmp/codex-code-mode-host",@[@"/tmp/codex-code-mode-host"],@"",bundle,home,NO),@"unknown");
    NSString *cwd = [home stringByAppendingPathComponent:@".codex/plugins/cache/openai-bundled/codex-app-tools/26.1"];
    equal(@"exact app tools server",CMMProcessClass(node,@[node,@"./server.mjs"],cwd,bundle,home,NO),@"codex_app_tools");
    equal(@"arbitrary server blocks",CMMProcessClass(node,@[node,@"./server.mjs"],@"/tmp",bundle,home,NO),@"unknown");
    equal(@"app tools extra flags block",CMMProcessClass(node,@[node,@"--eval",@"work"],cwd,bundle,home,NO),@"unknown");
    NSString *preload = @"file:///Users/test/.codex/plugins/cache/openai-bundled/unified-computer-use/26.1/require-browser-access.mjs";
    NSString *cua = [bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/lib/node_modules/@oai/cua-repl/bin/cua-repl.mjs"];
    equal(@"exact CUA host",CMMProcessClass(node,@[node,@"--import",preload,cua],@"",bundle,home,NO),@"cua_repl");
    equal(@"unknown CUA import blocks",CMMProcessClass(node,@[node,@"--import",@"file:///tmp/require-browser-access.mjs",cua],@"",bundle,home,NO),@"unknown");
    equal(@"direct plain shell candidate",CMMProcessClass(@"/bin/zsh",@[@"/bin/zsh"],@"",bundle,home,YES),@"terminal_candidate");
    equal(@"direct login shell candidate",CMMProcessClass(@"/bin/zsh",@[@"/bin/zsh",@"-l"],@"",bundle,home,YES),@"terminal_candidate");
    equal(@"executing shell blocks",CMMProcessClass(@"/bin/zsh",@[@"/bin/zsh",@"-c",@"sleep 100"],@"",bundle,home,YES),@"unknown");
    equal(@"script shell blocks",CMMProcessClass(@"/bin/zsh",@[@"/bin/zsh",@"script.sh"],@"",bundle,home,YES),@"unknown");
    equal(@"nested interactive shell blocks",CMMProcessClass(@"/bin/zsh",@[@"/bin/zsh"],@"",bundle,home,NO),@"unknown");
    NSMutableDictionary *terminalState = [NSMutableDictionary dictionary];
    NSDictionary *terminal = @{@"terminals":@[@{@"identity":@"123:12300000",@"cpu_ns":@1000}]};
    equal(@"terminal needs window",@(CMMUpdateTerminalQuiet(terminal,terminalState,cfg,1000,boot)),@NO);
    for (int n=1; n<15; n++) CMMUpdateTerminalQuiet(terminal,terminalState,cfg,1000+n*60,boot);
    equal(@"terminal stable whole window",@(CMMUpdateTerminalQuiet(terminal,terminalState,cfg,1900,boot)),@YES);
    NSDictionary *changed = @{@"terminals":@[@{@"identity":@"123:12300000",@"cpu_ns":@1001}]};
    equal(@"terminal CPU change resets",@(CMMUpdateTerminalQuiet(changed,terminalState,cfg,1960,boot)),@NO);
    equal(@"terminal missed samples reset",@(CMMUpdateTerminalQuiet(changed,terminalState,cfg,2500,boot)),@NO);
    equal(@"terminal reboot resets",@(CMMUpdateTerminalQuiet(changed,terminalState,cfg,2560,boot+1)),@NO);
    equal(@"terminal clock rollback resets",@(CMMUpdateTerminalQuiet(changed,terminalState,cfg,10,boot+1)),@NO);
    equal(@"no terminals needs no terminal window",@(CMMUpdateTerminalQuiet(@{},terminalState,cfg,70,boot+1)),@YES);
    equal(@"missing terminal drops prior history",terminalState[@"terminal_quiet"],@{});
    printf("%d policy/dialog/process-classification checks passed; no live AX action performed\n",checks);
    return 0;
} }
