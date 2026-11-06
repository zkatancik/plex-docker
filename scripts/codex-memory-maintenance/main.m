#include "terminal-state.h"
#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>
#import <Security/Security.h>
#import <CommonCrypto/CommonDigest.h>
#import "policy.h"
#include <libproc.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/sysctl.h>
#include <sys/proc.h>
#include <time.h>
#include <unistd.h>
#include <errno.h>

static NSString *privateDir;
static double epoch(void) { return NSDate.date.timeIntervalSince1970; }
static double monotonic(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC_RAW, &t); return t.tv_sec + t.tv_nsec / 1e9; }
static long long bootTime(void) {
    struct timeval t; size_t size = sizeof(t);
    return sysctlbyname("kern.boottime", &t, &size, NULL, 0) == 0 ? t.tv_sec : 0;
}
static double idleTime(void) {
    return CGEventSourceSecondsSinceLastEventType(kCGEventSourceStateCombinedSessionState, kCGAnyInputEventType);
}
static BOOL regularOwned(NSString *path) {
    struct stat s;
    return lstat(path.fileSystemRepresentation, &s) == 0 && S_ISREG(s.st_mode) &&
           s.st_uid == getuid() && !(s.st_mode & 0022);
}
static id readJSON(NSString *path, NSUInteger maximum) {
    if (!regularOwned(path)) return nil;
    NSDictionary *a = [NSFileManager.defaultManager attributesOfItemAtPath:path error:nil];
    if ([a[NSFileSize] unsignedLongLongValue] > maximum) return nil;
    NSData *data = [NSData dataWithContentsOfFile:path];
    return data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
}
static BOOL saveJSON(NSString *path, id value) {
    NSData *data = [NSJSONSerialization dataWithJSONObject:value options:NSJSONWritingPrettyPrinted error:nil];
    NSString *tmp = [path stringByAppendingFormat:@".%d.tmp", getpid()];
    int fd = open(tmp.fileSystemRepresentation, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0600);
    if (fd < 0 || !data) { if (fd >= 0) close(fd); return NO; }
    const char *p = data.bytes; size_t left = data.length;
    while (left) { ssize_t n = write(fd, p, left); if (n <= 0) { close(fd); unlink(tmp.fileSystemRepresentation); return NO; } left -= n; p += n; }
    BOOL ok = fsync(fd) == 0; close(fd);
    if (ok) ok = rename(tmp.fileSystemRepresentation, path.fileSystemRepresentation) == 0;
    if (ok) {
        int directory = open(path.stringByDeletingLastPathComponent.fileSystemRepresentation, O_RDONLY | O_DIRECTORY | O_NOFOLLOW);
        ok = directory >= 0 && fsync(directory) == 0;
        if (directory >= 0) close(directory);
    }
    if (!ok) unlink(tmp.fileSystemRepresentation);
    return ok;
}
static id axValue(AXUIElementRef e, CFStringRef key) {
    CFTypeRef value = NULL;
    return AXUIElementCopyAttributeValue(e, key, &value) == kAXErrorSuccess ? CFBridgingRelease(value) : nil;
}
static BOOL sameProcess(pid_t pid, NSString *path, unsigned long long start) {
    struct proc_bsdinfo b; char buf[PROC_PIDPATHINFO_MAXSIZE] = {0};
    return proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &b, sizeof(b)) == sizeof(b) &&
           proc_pidpath(pid, buf, sizeof(buf)) > 0 &&
           [path isEqual:[NSString stringWithUTF8String:buf]] && (b.pbi_start_tvsec * 1000000ULL + b.pbi_start_tvusec) == start;
}
static NSString *originalIdentity(NSDictionary *pending, NSDictionary *cfg) {
    if (bootTime() <= 0) return @"unknown";
    if ([pending[@"boot_time"] longLongValue] != bootTime()) return @"gone";
    pid_t pid = [pending[@"old_pid"] intValue]; struct proc_bsdinfo b;
    if (proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, &b, sizeof(b)) != sizeof(b))
        return (kill(pid, 0) == -1 && errno == ESRCH) ? @"gone" : @"unknown";
    unsigned long long start = b.pbi_start_tvsec * 1000000ULL + b.pbi_start_tvusec;
    if (start != [pending[@"old_start"] unsignedLongLongValue]) return @"gone";
    char path[PROC_PIDPATHINFO_MAXSIZE] = {0};
    if (proc_pidpath(pid, path, sizeof(path)) <= 0) return @"unknown";
    return [cfg[@"target_executable"] isEqual:@(path)] ? @"same" : @"gone";
}
static NSString *packageFingerprint(NSString *bundle) {
    struct stat s;
    NSString *path = [bundle stringByAppendingPathComponent:@"Contents/Resources/app.asar"];
    if (lstat(path.fileSystemRepresentation, &s) || !S_ISREG(s.st_mode)) return nil;
    return [NSString stringWithFormat:@"%llu:%llu:%llu:%lld:%ld", (unsigned long long)s.st_dev,
            (unsigned long long)s.st_ino, (unsigned long long)s.st_size,
            (long long)s.st_mtimespec.tv_sec, s.st_mtimespec.tv_nsec];
}
static NSArray *processArguments(pid_t pid) {
    int mib[] = {CTL_KERN, KERN_PROCARGS2, pid}; size_t size = 512 * 1024;
    char *buf = calloc(1,size); if (!buf) return nil;
    if (sysctl(mib,3,buf,&size,NULL,0) || size <= sizeof(int)) { free(buf); return nil; }
    int count; memcpy(&count,buf,sizeof(count));
    if (count < 1 || count > 4096) { free(buf); return nil; }
    char *at = buf + sizeof(int), *end = buf + size;
    char *zero = memchr(at,0,end-at); if (!zero) { free(buf); return nil; }
    at = zero+1; while (at < end && *at == 0) at++;
    NSMutableArray *args = [NSMutableArray array];
    for (int n = 0; n < count; n++) {
        zero = at < end ? memchr(at,0,end-at) : NULL;
        if (!zero) { free(buf); return nil; }
        NSString *arg = [[NSString alloc] initWithBytes:at length:zero-at encoding:NSUTF8StringEncoding];
        if (!arg) { free(buf); return nil; }
        [args addObject:arg]; at = zero+1;
    }
    volatile char *clear = buf; for (size_t n = 0; n < size; n++) clear[n] = 0;
    free(buf); return args;
}
static NSString *processCWD(pid_t pid) {
    struct proc_vnodepathinfo info;
    return proc_pidinfo(pid,PROC_PIDVNODEPATHINFO,0,&info,sizeof(info)) == sizeof(info)
        ? @(info.pvi_cdir.vip_path) : nil;
}
static BOOL trustedUserFile(NSString *path) {
    if (!path || ![path.stringByStandardizingPath isEqual:path] ||
        ![path.stringByResolvingSymlinksInPath isEqual:path] || !regularOwned(path)) return NO;
    NSString *home = NSHomeDirectory();
    if (![path hasPrefix:[home stringByAppendingString:@"/"]]) return NO;
    for (NSString *dir = path.stringByDeletingLastPathComponent; dir.length >= home.length; dir = dir.stringByDeletingLastPathComponent) {
        struct stat st;
        if (lstat(dir.fileSystemRepresentation,&st) || !S_ISDIR(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 0022)) return NO;
        if ([dir isEqual:home]) return YES;
    }
    return NO;
}
static BOOL signedBundle(NSString *bundle, NSString *identifier) {
    SecStaticCodeRef code = NULL; SecRequirementRef requirement = NULL;
    NSString *rule = [NSString stringWithFormat:@"identifier \"%@\" and anchor apple generic and certificate leaf[subject.OU] = \"2DC432GLL2\"",identifier];
    if (SecRequirementCreateWithString((__bridge CFStringRef)rule,kSecCSDefaultFlags,&requirement) != errSecSuccess) return NO;
    if (SecStaticCodeCreateWithPath((__bridge CFURLRef)[NSURL fileURLWithPath:bundle],kSecCSDefaultFlags,&code) != errSecSuccess) { CFRelease(requirement); return NO; }
    BOOL ok = SecStaticCodeCheckValidity(code,kSecCSCheckAllArchitectures | kSecCSStrictValidate,requirement) == errSecSuccess;
    CFRelease(requirement); CFRelease(code); return ok;
}
static NSDictionary *processSnapshot(pid_t mainPID, NSString *bundle) {
    NSMutableDictionary *all = [NSMutableDictionary dictionary];
    NSMutableArray *queue = [NSMutableArray arrayWithObject:@(mainPID)];
    for (NSUInteger i = 0; i < queue.count; i++) {
        if (queue.count > 4096) return @{@"ok": @NO, @"reason": @"process_tree_too_large"};
        pid_t pid = [queue[i] intValue]; struct proc_bsdinfo b;
        if (proc_pidinfo(pid,PROC_PIDTBSDINFO,0,&b,sizeof(b)) != sizeof(b))
            return @{@"ok": @NO, @"reason": @"descendant_census_incomplete"};
        pid_t children[4096]; errno = 0;
        int count = proc_listchildpids(pid,children,sizeof(children));
        if (count < 0 || count >= 4096 || (count == 0 && errno))
            return @{@"ok": @NO, @"reason": @"descendant_list_unavailable"};
        for (int n = 0; n < count; n++) if (children[n] > 0 && ![queue containsObject:@(children[n])]) [queue addObject:@(children[n])];
        char buf[PROC_PIDPATHINFO_MAXSIZE] = {0};
        NSArray *args = processArguments(pid);
        if (proc_pidpath(pid,buf,sizeof(buf)) <= 0 || !args || b.pbi_uid != getuid())
            return @{@"ok": @NO, @"reason": @"descendant_identity_unknown"};
        all[@(pid)] = @{@"ppid": @(b.pbi_ppid), @"start": @(b.pbi_start_tvsec * 1000000ULL + b.pbi_start_tvusec),
            @"path": @(buf), @"argv": args, @"children": @(count), @"bsd_status": @(b.pbi_status),
            @"foreground_tty": @((b.pbi_flags & PROC_FLAG_CTTY) && b.e_tdev != UINT32_MAX && b.e_tpgid == b.pbi_pgid)};
    }
    NSMutableArray *unknown = [NSMutableArray array], *signature = [NSMutableArray array], *terminals = [NSMutableArray array];
    NSMutableDictionary *classes = [NSMutableDictionary dictionary]; int servers = 0;
    for (NSNumber *pid in queue) {
        NSDictionary *item = all[pid]; NSString *p = item[@"path"]; NSArray *args = item[@"argv"];
        BOOL direct = [item[@"ppid"] intValue] == mainPID;
        NSString *identity = [NSString stringWithFormat:@"%@:%@",pid,item[@"start"]];
        if (pid.intValue != mainPID) {
            NSString *kind = CMMProcessClass(p,args,processCWD(pid.intValue) ?: @"",bundle,NSHomeDirectory(),direct);
            if ([kind isEqual:@"unknown"]) {
                NSDictionary *parent = all[item[@"ppid"]];
                NSDictionary *grandparent = parent ? all[parent[@"ppid"]] : nil;
                NSString *grandClass = grandparent ? CMMProcessClass(grandparent[@"path"],grandparent[@"argv"],
                    processCWD([parent[@"ppid"] intValue]) ?: @"",bundle,NSHomeDirectory(),[grandparent[@"ppid"] intValue] == mainPID) : @"unknown";
                if ([grandClass isEqual:@"cua_repl"] && parent) {
                    if (CMMVendorWorker(p,args,parent[@"path"],parent[@"argv"],bundle)) kind = @"vendor_repl_worker";
                    else if (CMMNestedStdioServer(p,args,parent[@"path"],parent[@"argv"],bundle)) kind = @"vendor_stdio_server";
                }
            }
            BOOL ok = ![kind isEqual:@"unknown"];
            if ([kind isEqual:@"app_server"]) servers++;
            if ([kind isEqual:@"code_mode_candidate"]) {
                NSDictionary *parent = all[item[@"ppid"]];
                ok = parent && [CMMProcessClass(parent[@"path"],parent[@"argv"],@"",bundle,NSHomeDirectory(),
                    [parent[@"ppid"] intValue] == mainPID) isEqual:@"app_server"] && signedBundle(p,@"codex-code-mode-host");
                if (ok) kind = @"signed_code_mode_host";
            }
            if ([kind isEqual:@"codex_app_tools"]) ok = trustedUserFile([processCWD(pid.intValue) stringByAppendingPathComponent:@"server.mjs"]);
            if ([kind isEqual:@"cua_repl"]) ok = trustedUserFile([NSURL URLWithString:args[2]].path);
            if ([kind isEqual:@"artifact_host"]) ok = trustedUserFile(p) && trustedUserFile(args[1]);
            if ([kind isEqual:@"signed_computer_use"]) ok = signedBundle([NSHomeDirectory() stringByAppendingPathComponent:@".codex/computer-use/Codex Computer Use.app"],@"com.openai.sky.CUAService");
            if ([kind isEqual:@"terminal_candidate"]) {
                struct proc_taskinfo task;
                ok = [item[@"children"] integerValue] == 0 && [item[@"foreground_tty"] boolValue] &&
                    CMMTerminalWaitingSnapshot(pid.intValue, [item[@"start"] unsignedLongLongValue], &task, proc_pidinfo);
                if (ok) {
                    NSNumber *cpu = @(task.pti_total_user + task.pti_total_system);
                    [terminals addObject:@{@"identity": identity,@"cpu_ns": cpu}];
                    identity = [identity stringByAppendingFormat:@":cpu=%@",cpu];
                }
            }
            if (!ok) [unknown addObject:p.lastPathComponent ?: @"unknown"];
            classes[kind] = @([classes[kind] integerValue]+1);
        }
        [signature addObject:identity];
    }
    [signature sortUsingSelector:@selector(compare:)];
    return @{@"ok": @(unknown.count == 0 && servers == 1),
             @"reason": unknown.count ? @"unqualified_tool_or_external_descendant" : (servers == 1 ? @"ok" : @"app_server_inventory_unknown"),
             @"unknown_basenames": [[NSSet setWithArray:unknown] allObjects], @"signature": signature,
             @"main_start": all[@(mainPID)][@"start"], @"count": @(queue.count), @"terminals": terminals, @"classes": classes};
}
static BOOL validateIdentity(NSDictionary *cfg, NSString *fingerprint) {
    NSString *bundle = cfg[@"target_bundle"];
    NSDictionary *info = [NSDictionary dictionaryWithContentsOfFile:[bundle stringByAppendingPathComponent:@"Contents/Info.plist"]];
    if (![info[@"CFBundleIdentifier"] isEqual:cfg[@"target_bundle_id"]]) return NO;
    SecStaticCodeRef code = NULL; SecRequirementRef requirement = NULL;
    NSString *rule = @"identifier \"com.openai.codex\" and anchor apple generic and certificate leaf[subject.OU] = \"2DC432GLL2\"";
    if (SecRequirementCreateWithString((__bridge CFStringRef)rule, kSecCSDefaultFlags, &requirement) != errSecSuccess) return NO;
    if (SecStaticCodeCreateWithPath((__bridge CFURLRef)[NSURL fileURLWithPath:bundle], kSecCSDefaultFlags, &code) != errSecSuccess) { CFRelease(requirement); return NO; }
    BOOL ok = SecStaticCodeCheckValidity(code, kSecCSCheckAllArchitectures | kSecCSStrictValidate, requirement) == errSecSuccess;
    CFRelease(requirement); CFRelease(code);
    // Official signed updates are allowed. A stable fingerprint protects this action boundary;
    // initial version, CDHash, and package SHA remain diagnostic baseline data in config.
    return ok && fingerprint && [fingerprint isEqual:packageFingerprint(bundle)];
}
static NSArray *history(NSString *dir) {
    NSArray *names = [[NSFileManager.defaultManager contentsOfDirectoryAtPath:dir error:nil] sortedArrayUsingSelector:@selector(compare:)];
    NSMutableArray *files = [NSMutableArray array];
    NSRegularExpression *pattern = [NSRegularExpression regularExpressionWithPattern:@"^history-[0-9]{8}\\.jsonl$" options:0 error:nil];
    for (NSString *name in names) if ([pattern numberOfMatchesInString:name options:0 range:NSMakeRange(0, name.length)]) [files addObject:name];
    if (files.count > 2) [files removeObjectsInRange:NSMakeRange(0, files.count - 2)];
    NSMutableArray *samples = [NSMutableArray array]; NSUInteger bytes = 0;
    for (NSString *name in files) {
        NSString *path = [dir stringByAppendingPathComponent:name];
        if (!regularOwned(path)) return nil;
        NSDictionary *attributes = [NSFileManager.defaultManager attributesOfItemAtPath:path error:nil];
        if (!attributes || [attributes[NSFileSize] unsignedLongLongValue] > 32 * 1024 * 1024 - bytes) return nil;
        NSData *data = [NSData dataWithContentsOfFile:path]; bytes += data.length;
        if (!data || bytes > 32 * 1024 * 1024) return nil;
        NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding]; if (!text) return nil;
        for (NSString *line in [text componentsSeparatedByString:@"\n"]) {
            if (!line.length) continue;
            if (line.length > 65536) return nil;
            id sample = [NSJSONSerialization JSONObjectWithData:[line dataUsingEncoding:NSUTF8StringEncoding] options:0 error:nil];
            if (![sample isKindOfClass:NSDictionary.class]) return nil;
            [samples addObject:sample];
        }
    }
    return samples;
}
static BOOL configValid(NSDictionary *cfg) {
    if (![cfg isKindOfClass:NSDictionary.class] || ![cfg[@"schema_version"] isEqual:@1]) return NO;
    if (!cfg[@"enabled"] || CFGetTypeID((__bridge CFTypeRef)cfg[@"enabled"]) != CFBooleanGetTypeID()) return NO;
    for (NSString *k in @[@"target_bundle", @"target_bundle_id", @"target_executable", @"target_version", @"target_build", @"target_team_id", @"target_cdhash", @"target_asar_sha256", @"telemetry_directory"])
        if (![cfg[k] isKindOfClass:NSString.class] || ![cfg[k] length]) return NO;
    for (NSString *k in @[@"max_sample_age_seconds", @"sustained_seconds", @"min_footprint_mib", @"min_app_uptime_seconds", @"min_user_idle_seconds", @"cooldown_seconds", @"attempt_cooldown_seconds", @"normal_exit_timeout_seconds", @"relaunch_timeout_seconds", @"max_relaunch_attempts", @"relaunch_retry_seconds"])
        if (![cfg[k] isKindOfClass:NSNumber.class] || !isfinite([cfg[k] doubleValue]) || [cfg[k] doubleValue] <= 0) return NO;
    return [cfg[@"target_bundle_id"] isEqual:@"com.openai.codex"] &&
           [cfg[@"target_bundle"] isEqual:@"/Applications/ChatGPT.app"] &&
           [cfg[@"target_executable"] isEqual:@"/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"] &&
           [cfg[@"target_asar_sha256"] length] == 64 && [cfg[@"max_sample_age_seconds"] doubleValue] <= 150 &&
           [cfg[@"normal_exit_timeout_seconds"] doubleValue] <= 120 && [cfg[@"relaunch_timeout_seconds"] doubleValue] <= 120;
}
static void record(NSMutableDictionary *state, NSString *reason, NSDictionary *extra, BOOL dry) {
    NSMutableDictionary *entry = [@{@"schema_version": @1, @"sampled_at_epoch": @(epoch()), @"result": reason,
                                   @"accessibility_trusted": @(AXIsProcessTrusted())} mutableCopy];
    [entry addEntriesFromDictionary:extra ?: @{}];
    if (dry) {
        NSData *d = [NSJSONSerialization dataWithJSONObject:entry options:NSJSONWritingPrettyPrinted error:nil];
        puts([[NSString alloc] initWithData:d encoding:NSUTF8StringEncoding].UTF8String); return;
    }
    state[@"last_result"] = entry;
    saveJSON([privateDir stringByAppendingPathComponent:@"state.json"], state);
    NSString *path = [privateDir stringByAppendingPathComponent:@"history.json"];
    id existing = readJSON(path, 1024 * 1024);
    NSMutableArray *entries = [existing isKindOfClass:NSArray.class] ? [existing mutableCopy] : [NSMutableArray array];
    [entries addObject:entry]; while (entries.count > 256) [entries removeObjectAtIndex:0];
    saveJSON(path, entries);
}
static void walk(AXUIElementRef element, NSMutableArray *out, NSUInteger depth, NSUInteger *budget) {
    if (!element || depth > 12 || *budget == 0) return;
    (*budget)--; [out addObject:(__bridge id)element];
    id children = axValue(element, kAXChildrenAttribute);
    if ([children isKindOfClass:NSArray.class]) for (id child in children) walk((__bridge AXUIElementRef)child, out, depth + 1, budget);
}
static NSArray *dialogWindows(AXUIElementRef app) {
    id windows = axValue(app, kAXWindowsAttribute); if (![windows isKindOfClass:NSArray.class]) return nil;
    NSMutableOrderedSet *dialogs = [NSMutableOrderedSet orderedSet];
    for (id w in windows) {
        // Native alerts are top-level modal windows or sheets. Do not traverse a chat's
        // potentially huge web accessibility tree and confuse its size with unknown UI.
        NSMutableArray *nodes = [NSMutableArray arrayWithObject:w];
        id children = axValue((__bridge AXUIElementRef)w,kAXChildrenAttribute);
        if ([children isKindOfClass:NSArray.class]) [nodes addObjectsFromArray:children];
        if (nodes.count > 100) return nil;
        for (id node in nodes) {
            NSString *role = axValue((__bridge AXUIElementRef)node, kAXRoleAttribute);
            NSString *sub = axValue((__bridge AXUIElementRef)node, kAXSubroleAttribute);
            if ([role isEqual:@"AXSheet"] || [sub isEqual:@"AXDialog"] || [sub isEqual:@"AXSystemDialog"] ||
                [axValue((__bridge AXUIElementRef)node,kAXModalAttribute) boolValue])
                [dialogs addObject:node];
        }
    }
    return dialogs.array;
}
static NSDictionary *dialogData(AXUIElementRef dialog) {
    NSMutableArray *nodes = [NSMutableArray array], *texts = [NSMutableArray array], *buttons = [NSMutableArray array];
    NSMutableDictionary *actions = [NSMutableDictionary dictionary]; NSUInteger budget = 150;
    walk(dialog, nodes, 0, &budget); if (!budget) return nil;
    for (id obj in nodes) {
        AXUIElementRef e = (__bridge AXUIElementRef)obj;
        NSString *role = axValue(e, kAXRoleAttribute);
        if ([role isEqual:@"AXStaticText"]) {
            for (id key in @[(__bridge id)kAXValueAttribute, (__bridge id)kAXTitleAttribute]) {
                id value = axValue(e, (__bridge CFStringRef)key); if ([value isKindOfClass:NSString.class] && [value length]) [texts addObject:value];
            }
        } else if ([role isEqual:@"AXButton"]) {
            id title = axValue(e, kAXTitleAttribute);
            if ([title isKindOfClass:NSString.class]) { [buttons addObject:title]; actions[title] = obj; }
        }
    }
    return @{@"texts": texts, @"buttons": buttons, @"actions": actions};
}
static BOOL cancelDialog(AXUIElementRef dialog, NSDictionary *data) {
    // Never dismiss a foreign approval or document dialog as though it were ours.
    if (![data[@"texts"] containsObject:@"Quit ChatGPT?"] ||
        ![data[@"buttons"] containsObject:@"Quit"] || ![data[@"buttons"] containsObject:@"Cancel"]) return NO;
    id cancel = axValue(dialog, kAXCancelButtonAttribute) ?: data[@"actions"][@"Cancel"];
    return cancel && AXUIElementPerformAction((__bridge AXUIElementRef)cancel, kAXPressAction) == kAXErrorSuccess;
}
static BOOL noDialogsAfterCancel(AXUIElementRef app) {
    double until = monotonic() + 2;
    while (monotonic() < until) {
        NSArray *dialogs = dialogWindows(app);
        if (dialogs && dialogs.count == 0) return YES;
        usleep(100000);
    }
    return NO;
}
static BOOL eligibilityStillHolds(NSDictionary *cfg, NSDictionary *state, pid_t pid) {
    NSString *dir = [cfg[@"telemetry_directory"] stringByExpandingTildeInPath];
    id latest = readJSON([dir stringByAppendingPathComponent:@"latest.json"], 65536);
    NSArray *samples = history(dir);
    NSMutableDictionary *check = [state mutableCopy]; [check removeObjectForKey:@"pending_restart"];
    return samples && [latest isKindOfClass:NSDictionary.class] &&
        [CMMPolicy(cfg, samples, latest, check, epoch(), monotonic(), idleTime(), bootTime()) isEqual:@"eligible"] &&
        [latest[@"codex"][@"main_pid"] intValue] == pid;
}
static void markSuccess(NSMutableDictionary *state, pid_t newPID) {
    state[@"last_success_epoch"] = @(epoch()); state[@"last_success_monotonic"] = @(monotonic());
    state[@"last_success_boot"] = @(bootTime()); state[@"last_success_new_pid"] = @(newPID);
    state[@"last_success_clock"] = @"CLOCK_MONOTONIC_RAW";
    [state removeObjectForKey:@"pending_restart"];
}
static BOOL replacementVerified(NSRunningApplication *app, NSDictionary *cfg) {
    return app && !app.terminated && app.finishedLaunching && [app.executableURL.path isEqual:cfg[@"target_executable"]] &&
        validateIdentity(cfg, packageFingerprint(cfg[@"target_bundle"]));
}
static NSString *recoverRestart(NSDictionary *cfg, NSMutableDictionary *state, BOOL dry) {
    id raw = state[@"pending_restart"];
    if (![raw isKindOfClass:NSDictionary.class] || ![raw[@"schema_version"] isEqual:@1] ||
        ![raw[@"old_pid"] isKindOfClass:NSNumber.class] || ![raw[@"old_start"] isKindOfClass:NSNumber.class] ||
        ![raw[@"boot_time"] isKindOfClass:NSNumber.class]) return @"pending_receipt_invalid";
    NSMutableDictionary *pending = [raw mutableCopy];
    NSString *original = originalIdentity(pending,cfg);
    if ([original isEqual:@"same"] && [pending[@"phase"] isEqual:@"quit_requested"] && AXIsProcessTrusted()) {
        // Recover after this helper itself died while its normal Quit warning was open.
        // Never accept a recovery dialog: cancellation preserves work and restores operation.
        AXUIElementRef app = AXUIElementCreateApplication([pending[@"old_pid"] intValue]);
        AXUIElementSetMessagingTimeout(app,2);
        NSArray *dialogs = dialogWindows(app); BOOL allOwned = dialogs.count > 0;
        for (id dialog in dialogs) {
            NSDictionary *data = dialogData((__bridge AXUIElementRef)dialog);
            allOwned &= [data[@"texts"] containsObject:@"Quit ChatGPT?"] &&
                [data[@"buttons"] containsObject:@"Quit"] && [data[@"buttons"] containsObject:@"Cancel"];
        }
        if (allOwned && !dry) {
            BOOL cancelled = YES;
            for (id dialog in dialogs) cancelled &= cancelDialog((__bridge AXUIElementRef)dialog,dialogData((__bridge AXUIElementRef)dialog));
            if (cancelled && noDialogsAfterCancel(app) && [originalIdentity(pending,cfg) isEqual:@"same"]) {
                [state removeObjectForKey:@"pending_restart"]; CFRelease(app); return @"recovered_quit_dialog_cancelled";
            }
        }
        CFRelease(app);
        if (allOwned) return dry ? @"dry_recovery:cancel_owned_quit_dialog" : @"recovery_dialog_cancel_unconfirmed";
    }
    NSArray<NSRunningApplication *> *existing = [NSRunningApplication runningApplicationsWithBundleIdentifier:cfg[@"target_bundle_id"]];
    BOOL verified = [original isEqual:@"gone"] && existing.count == 1 && replacementVerified(existing[0],cfg);
    double elapsed = epoch() - [pending[@"last_launch_epoch"] doubleValue];
    if ([pending[@"last_launch_boot"] longLongValue] == bootTime() && pending[@"last_launch_monotonic"]) {
        double m = monotonic() - [pending[@"last_launch_monotonic"] doubleValue];
        if (fabs(elapsed-m) > 10) elapsed = -1;
    }
    NSString *decision = CMMRecoveryDecision(original,existing.count,verified,[pending[@"launch_attempts"] unsignedIntegerValue],
        elapsed,[cfg[@"max_relaunch_attempts"] unsignedIntegerValue],[cfg[@"relaunch_retry_seconds"] doubleValue]);
    if (dry) return [@"dry_recovery:" stringByAppendingString:decision];
    if ([decision isEqual:@"recognize_replacement"]) {
        markSuccess(state,existing[0].processIdentifier); return @"replacement_process_recognized";
    }
    if (![decision isEqual:@"relaunch_confirmed_exit"]) return decision;
    // This also qualifies an official update installed while the original app was quitting.
    if (!validateIdentity(cfg,packageFingerprint(cfg[@"target_bundle"]))) return @"relaunch_code_identity_unverified";
    if ([NSRunningApplication runningApplicationsWithBundleIdentifier:cfg[@"target_bundle_id"]].count)
        return @"replacement_appeared_before_launch";
    pending[@"launch_attempts"] = @([pending[@"launch_attempts"] unsignedIntegerValue] + 1);
    pending[@"last_launch_epoch"] = @(epoch()); pending[@"last_launch_monotonic"] = @(monotonic());
    pending[@"last_launch_boot"] = @(bootTime()); pending[@"phase"] = @"launch_requested";
    state[@"pending_restart"] = pending;
    if (!saveJSON([privateDir stringByAppendingPathComponent:@"state.json"],state)) return @"could_not_persist_launch_intent";
    NSWorkspaceOpenConfiguration *options = NSWorkspaceOpenConfiguration.configuration;
    options.activates = NO; options.createsNewApplicationInstance = NO;
    __block BOOL complete = NO;
    [NSWorkspace.sharedWorkspace openApplicationAtURL:[NSURL fileURLWithPath:cfg[@"target_bundle"]] configuration:options
        completionHandler:^(NSRunningApplication *a, NSError *error) { (void)a; (void)error; complete = YES; }];
    double deadline = monotonic() + [cfg[@"relaunch_timeout_seconds"] doubleValue];
    while (!complete && monotonic() < deadline)
        [NSRunLoop.currentRunLoop runUntilDate:[NSDate dateWithTimeIntervalSinceNow:0.1]];
    existing = [NSRunningApplication runningApplicationsWithBundleIdentifier:cfg[@"target_bundle_id"]];
    if (existing.count == 1 && replacementVerified(existing[0],cfg)) {
        markSuccess(state,existing[0].processIdentifier); return @"normal_restart_process_verified";
    }
    return @"relaunch_pending_bounded_recovery";
}
static NSString *performRestart(NSDictionary *cfg, NSMutableDictionary *state, NSDictionary *snapshot) {
    pid_t pid = [state[@"pending_restart"][@"old_pid"] intValue];
    unsigned long long start = [snapshot[@"main_start"] unsignedLongLongValue];
    NSString *exe = cfg[@"target_executable"], *bundle = cfg[@"target_bundle"];
    AXUIElementRef app = AXUIElementCreateApplication(pid);
    AXUIElementSetMessagingTimeout(app, 2);
    NSArray *before = dialogWindows(app);
    if (!before || before.count) { CFRelease(app); return @"existing_or_unknown_dialog"; }
    id menu = axValue(app, kAXMenuBarAttribute); NSMutableArray *nodes = [NSMutableArray array]; NSUInteger budget = 600;
    if (menu) walk((__bridge AXUIElementRef)menu, nodes, 0, &budget);
    NSMutableArray *matches = [NSMutableArray array];
    for (id node in nodes) if ([axValue((__bridge AXUIElementRef)node, kAXRoleAttribute) isEqual:@"AXMenuItem"] &&
        [axValue((__bridge AXUIElementRef)node, kAXTitleAttribute) isEqual:@"Quit ChatGPT"] &&
        [axValue((__bridge AXUIElementRef)node, kAXEnabledAttribute) boolValue]) [matches addObject:node];
    if (!budget || matches.count != 1) { CFRelease(app); return @"normal_quit_menu_unknown"; }
    NSDictionary *check = processSnapshot(pid, bundle);
    if (![check[@"ok"] boolValue] || ![check[@"signature"] isEqual:snapshot[@"signature"]] ||
        idleTime() < [cfg[@"min_user_idle_seconds"] doubleValue] || !sameProcess(pid, exe, start) ||
        ![packageFingerprint(bundle) isEqual:snapshot[@"package_fingerprint"]] || !eligibilityStillHolds(cfg,state,pid)) {
        CFRelease(app); return @"activity_changed_before_quit";
    }
    // The normal menu invokes the app's native before-quit checks. No hidden IPC or signal is used.
    // AX may time out after delivery while the synchronous native quit alert is open.
    // An error is not proof that no request happened: observe the same bounded lifecycle.
    (void)AXUIElementPerformAction((__bridge AXUIElementRef)matches[0], kAXPressAction);
    double deadline = monotonic() + [cfg[@"normal_exit_timeout_seconds"] doubleValue]; BOOL accepted = NO;
    while (sameProcess(pid, exe, start) && monotonic() < deadline) {
        @autoreleasepool {
            NSArray *dialogs = dialogWindows(app);
            if (dialogs.count) {
                if (dialogs.count != 1) {
                    BOOL cancelled = YES;
                    for (id d in dialogs) cancelled &= cancelDialog((__bridge AXUIElementRef)d, dialogData((__bridge AXUIElementRef)d));
                    BOOL resolved = cancelled && noDialogsAfterCancel(app);
                    CFRelease(app); return resolved ? @"multiple_quit_dialogs_cancelled" : @"unknown_dialog_no_action";
                }
                AXUIElementRef d = (__bridge AXUIElementRef)dialogs[0]; NSDictionary *data = dialogData(d);
                if (accepted) { usleep(100000); continue; }
                NSDictionary *current = processSnapshot(pid, bundle);
                BOOL unchanged = [current[@"ok"] boolValue] && [current[@"signature"] isEqual:snapshot[@"signature"]] &&
                    idleTime() >= [cfg[@"min_user_idle_seconds"] doubleValue] && sameProcess(pid, exe, start) &&
                    [packageFingerprint(bundle) isEqual:snapshot[@"package_fingerprint"]] && eligibilityStillHolds(cfg,state,pid);
                NSString *decision = CMMDialogDecision(data[@"texts"] ?: @[], data[@"buttons"] ?: @[], YES, unchanged);
                if (![decision isEqual:@"accept_automations_only"]) {
                    BOOL cancelled = cancelDialog(d, data) && noDialogsAfterCancel(app);
                    CFRelease(app); return cancelled ? @"quit_guard_deferred_and_cancelled" : @"quit_dialog_cancel_unconfirmed";
                }
                id button = data[@"actions"][@"Quit"];
                if (!button) {
                    cancelDialog(d, data); CFRelease(app); return @"quit_confirmation_failed";
                }
                (void)AXUIElementPerformAction((__bridge AXUIElementRef)button, kAXPressAction);
                accepted = YES;
            }
        }
        usleep(200000);
    }
    if (sameProcess(pid, exe, start)) {
        NSArray *dialogs = dialogWindows(app); BOOL cancelled = dialogs.count > 0;
        for (id d in dialogs) cancelled &= cancelDialog((__bridge AXUIElementRef)d, dialogData((__bridge AXUIElementRef)d));
        if (cancelled && noDialogsAfterCancel(app)) { CFRelease(app); return @"quit_guard_deferred_and_cancelled"; }
        CFRelease(app); return @"normal_exit_not_confirmed_no_force";
    }
    CFRelease(app);
    return recoverRestart(cfg,state,NO);
}

int main(int argc, const char *argv[]) { @autoreleasepool {
    NSString *mode = argc > 1 ? @(argv[1]) : @"--dry-run";
    privateDir = [NSHomeDirectory() stringByAppendingPathComponent:@".config/codex-memory-maintenance"];
    if ([mode isEqual:@"--check-permissions"] || [mode isEqual:@"--request-accessibility"]) {
        BOOL prompt = [mode isEqual:@"--request-accessibility"];
        BOOL trusted = prompt ? AXIsProcessTrustedWithOptions((__bridge CFDictionaryRef)@{(__bridge NSString *)kAXTrustedCheckOptionPrompt: @YES}) : AXIsProcessTrusted();
        printf("{\"accessibility_trusted\":%s,\"prompt_requested\":%s}\n", trusted ? "true" : "false", prompt ? "true" : "false"); return 0;
    }
    BOOL dry = [mode isEqual:@"--dry-run"];
    if (!dry && ![mode isEqual:@"--run"]) { fputs("Use --dry-run, --run, --check-permissions or explicit --request-accessibility\n", stderr); return 2; }
    struct stat dirStat;
    if (lstat(privateDir.fileSystemRepresentation, &dirStat) || !S_ISDIR(dirStat.st_mode) ||
        dirStat.st_uid != getuid() || (dirStat.st_mode & 0077)) return 2;
    NSString *lockPath = [privateDir stringByAppendingPathComponent:@"execution.lock"];
    int lock = open(lockPath.fileSystemRepresentation, O_RDWR | O_CREAT | O_NOFOLLOW, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) { if (lock >= 0) close(lock); return 0; }
    NSDictionary *cfg = readJSON([privateDir stringByAppendingPathComponent:@"config.json"], 65536);
    NSString *statePath = [privateDir stringByAppendingPathComponent:@"state.json"];
    id loaded = readJSON(statePath, 1024 * 1024);
    BOOL stateExists = [NSFileManager.defaultManager fileExistsAtPath:statePath];
    NSMutableDictionary *state = [loaded isKindOfClass:NSDictionary.class] ? [loaded mutableCopy] : [@{@"schema_version": @1} mutableCopy];
    if (!configValid(cfg) || (stateExists && ![loaded isKindOfClass:NSDictionary.class])) {
        // Preserve corrupt state, especially an unrecognized pending restart receipt.
        if (dry) record(state, @"configuration_or_state_invalid", nil, YES);
        close(lock); return 2;
    }
    if (state[@"pending_restart"]) {
        record(state,recoverRestart(cfg,state,dry),nil,dry); close(lock); return 0;
    }
    // Pending recovery may relaunch without AX. Only new unattended attempts
    // require permission; avoid history/vendor scans while that action is impossible.
    if (!dry && !AXIsProcessTrusted()) {
        record(state, @"accessibility_permission_required", nil, NO); close(lock); return 0;
    }
    NSString *telemetry = [cfg[@"telemetry_directory"] stringByExpandingTildeInPath];
    struct stat telemetryStat;
    if (lstat(telemetry.fileSystemRepresentation, &telemetryStat) || !S_ISDIR(telemetryStat.st_mode) ||
        telemetryStat.st_uid != getuid() || (telemetryStat.st_mode & 0077)) {
        record(state, @"telemetry_directory_untrusted", nil, dry); close(lock); return 0;
    }
    id latestValue = readJSON([telemetry stringByAppendingPathComponent:@"latest.json"], 65536);
    NSDictionary *latest = [latestValue isKindOfClass:NSDictionary.class] ? latestValue : nil;
    NSArray *samples = history(telemetry);
    NSString *reason = samples ? CMMPolicy(cfg, samples, latest, state, epoch(), monotonic(), idleTime(), bootTime()) : @"telemetry_history_unavailable";
    pid_t pid = [latest[@"codex"][@"main_pid"] intValue]; NSDictionary *processes = pid > 1 ? processSnapshot(pid, cfg[@"target_bundle"]) : @{};
    NSMutableDictionary *extra = [@{@"policy_result": reason, @"process_result": processes[@"reason"] ?: @"unknown",
                                  @"unknown_descendants": processes[@"unknown_basenames"] ?: @[], @"process_classes": processes[@"classes"] ?: @{}} mutableCopy];
    if (dry && ![cfg[@"enabled"] boolValue]) {
        NSMutableDictionary *enabled = [cfg mutableCopy]; enabled[@"enabled"] = @YES;
        extra[@"if_enabled_policy_result"] = samples ? CMMPolicy(enabled,samples,latest,state,epoch(),monotonic(),idleTime(),bootTime()) : @"telemetry_history_unavailable";
    }
    BOOL terminalsReady = CMMUpdateTerminalQuiet(processes,state,cfg,monotonic(),bootTime());
    if ([reason isEqual:@"eligible"]) {
        NSString *fingerprint = packageFingerprint(cfg[@"target_bundle"]);
        if (![processes[@"ok"] boolValue]) reason = processes[@"reason"] ?: @"process_inventory_unknown";
        else if (!terminalsReady) reason = @"integrated_terminal_quiet_window_incomplete";
        else if (!AXIsProcessTrusted()) reason = @"accessibility_permission_required";
        else if (state[@"last_attempt_epoch"] && epoch() - [state[@"last_attempt_epoch"] doubleValue] < [cfg[@"attempt_cooldown_seconds"] doubleValue]) reason = @"attempt_cooldown";
        else if (!validateIdentity(cfg, fingerprint)) reason = @"pinned_code_identity_mismatch";
        else if (dry) reason = @"eligible_dry_run_no_action";
        else {
            NSMutableDictionary *qualified = [processes mutableCopy];
            if (fingerprint) qualified[@"package_fingerprint"] = fingerprint;
            state[@"last_attempt_epoch"] = @(epoch());
            state[@"pending_restart"] = @{@"schema_version": @1, @"old_pid": @(pid), @"old_start": processes[@"main_start"],
                @"started_epoch": @(epoch()), @"boot_time": @(bootTime()), @"phase": @"quit_requested", @"launch_attempts": @0};
            if (!saveJSON(statePath, state)) reason = @"could_not_persist_restart_intent";
            else {
                reason = performRestart(cfg, state, qualified);
                // Only clearly cancelled/pre-request failures can automatically try again.
                if ([@[@"existing_or_unknown_dialog", @"normal_quit_menu_unknown", @"activity_changed_before_quit",
                       @"quit_guard_deferred_and_cancelled", @"multiple_quit_dialogs_cancelled"] containsObject:reason])
                    [state removeObjectForKey:@"pending_restart"];
            }
        }
    }
    record(state, reason, extra, dry); close(lock); return 0;
} }
