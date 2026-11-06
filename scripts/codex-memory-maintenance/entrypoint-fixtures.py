#!/usr/bin/python3
"""Compile the actual main entry point against inert collaborators, then test routing.

No live app census, signature scan, telemetry, AX action, process launch, TCC
request, production file, or installed helper is used by the fixture executable.
Compiler execution is explicit; run only after the supervised workload slot ends.
"""
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'main.m'
MARKER = 'int main(int argc, const char *argv[]) { @autoreleasepool {'
main = SOURCE.read_text()
if main.count(MARKER) != 1:
    raise SystemExit('main entry point must be uniquely located')
entry = main[main.index(MARKER):].replace(MARKER, MARKER.replace(' main(', ' CMMActualMain('), 1)

prefix = r'''
#import <Foundation/Foundation.h>
#include <sys/stat.h>
#include <sys/file.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdio.h>
static NSString *privateDir;
static NSMutableDictionary *fixtureState;
static NSDictionary *fixtureConfig;
static BOOL fixtureTrusted, fixtureValid;
static int historyCalls, censusCalls, recoveryCalls, quitCalls, closeCalls, telemetryReads, identityCalls;
static NSString *recordedReason;
static BOOL recordedDry;
static NSMutableDictionary *recordedState;
static NSString *fixtureHome(void) { return @"/CMM-inert-fixture-no-files"; }
static int fixtureLstat(const char *path, struct stat *s) {
    (void)path; memset(s,0,sizeof(*s)); s->st_mode=S_IFDIR|0700; s->st_uid=getuid(); return 0;
}
static int fixtureOpen(const char *p,int flags,...) {(void)p;(void)flags;return 42;}
static int fixtureFlock(int f,int op) {(void)f;(void)op;return 0;}
static int fixtureClose(int f) {(void)f;closeCalls++;return 0;}
static BOOL fixtureAXTrusted(void) {return fixtureTrusted;}
static BOOL fixtureAXPrompt(const void *o) {(void)o;abort();return NO;}
static id readJSON(NSString *path, NSUInteger maximum) {
    (void)maximum;
    if ([path.lastPathComponent isEqual:@"config.json"]) return fixtureConfig;
    if ([path.lastPathComponent isEqual:@"state.json"]) return fixtureState;
    if ([path.lastPathComponent isEqual:@"latest.json"]) {telemetryReads++;return @{@"codex":@{@"main_pid":@123}};}
    abort();return nil;
}
static BOOL configValid(NSDictionary *cfg) {(void)cfg;return fixtureValid;}
static void record(NSMutableDictionary *state,NSString *reason,NSDictionary *extra,BOOL dry) {
    (void)extra;recordedReason=reason;recordedDry=dry;recordedState=[state mutableCopy];
}
static NSString *recoverRestart(NSDictionary *cfg,NSMutableDictionary *state,BOOL dry) {
    (void)cfg;(void)state;(void)dry;recoveryCalls++;return @"fixture_recovery_dispatched";
}
static NSArray *history(NSString *p) {(void)p;historyCalls++;return @[];}
static double epoch(void) {return 10000;}
static double monotonic(void) {return 10000;}
static double idleTime(void) {return 10000;}
static long long bootTime(void) {return 1;}
static NSString *CMMPolicy(NSDictionary *c,NSArray *a,NSDictionary *l,NSDictionary *s,double e,double m,double i,long long b) {
    (void)c;(void)a;(void)l;(void)s;(void)e;(void)m;(void)i;(void)b;return @"eligible";
}
static NSDictionary *processSnapshot(pid_t p,NSString *b) {
    (void)p;(void)b;censusCalls++;return @{@"ok":@NO,@"reason":@"fixture_active_work"};
}
static BOOL CMMUpdateTerminalQuiet(NSDictionary *p,NSMutableDictionary *s,NSDictionary *c,double m,long long b) {
    (void)p;(void)s;(void)c;(void)m;(void)b;return YES;
}
static NSString *packageFingerprint(NSString *b) {(void)b;return @"fixture_metadata";}
static BOOL validateIdentity(NSDictionary *c,NSString *p) {(void)c;(void)p;identityCalls++;return YES;}
static BOOL saveJSON(NSString *p,id s) {(void)p;(void)s;abort();return NO;}
static NSString *performRestart(NSDictionary *c,NSMutableDictionary *s,NSDictionary *p) {
    (void)c;(void)s;(void)p;quitCalls++;abort();return nil;
}
#define kAXTrustedCheckOptionPrompt CFSTR("fixture_prompt")
#define NSHomeDirectory fixtureHome
#define lstat fixtureLstat
#define open fixtureOpen
#define flock fixtureFlock
#define close fixtureClose
#define AXIsProcessTrusted fixtureAXTrusted
#define AXIsProcessTrustedWithOptions fixtureAXPrompt
'''

suffix = r'''
static int checks;
static void require(BOOL condition, const char *name) {
    checks++;if(!condition) {fprintf(stderr,"FAIL: %s\n",name);exit(1);}
}
static void reset(BOOL trusted) {
    fixtureTrusted=trusted;fixtureValid=YES;
    fixtureConfig=@{@"enabled":@YES,@"telemetry_directory":@"/CMM-inert-fixture-no-files/telemetry",@"target_bundle":@"/fake"};
    fixtureState=[@{@"schema_version":@1,@"terminal_quiet":@{@"unchanged":@YES}} mutableCopy];
    historyCalls=censusCalls=recoveryCalls=quitCalls=closeCalls=telemetryReads=identityCalls=0;
    recordedReason=nil;recordedState=nil;recordedDry=NO;
}
static int invoke(const char *mode) {const char *argv[]={"fixture",mode};return CMMActualMain(2,argv);}
int main(void) { @autoreleasepool {
    reset(NO);
    require(invoke("--run")==0,"missing permission returns normally");
    require([recordedReason isEqual:@"accessibility_permission_required"],"missing permission reason");
    require(historyCalls==0 && telemetryReads==0 && censusCalls==0 && identityCalls==0,"no telemetry/vendor/signature work without permission");
    require(recoveryCalls==0 && quitCalls==0 && closeCalls==1,"no action and lock closed");
    require(!recordedDry && [recordedState[@"terminal_quiet"] isEqual:fixtureState[@"terminal_quiet"]],"record silently preserving terminal state");

    reset(NO);fixtureState[@"pending_restart"]=@{@"phase":@"launch_requested"};
    require(invoke("--run")==0 && recoveryCalls==1,"pending launch recovery runs without permission");
    require([recordedReason isEqual:@"fixture_recovery_dispatched"] && historyCalls==0 && censusCalls==0,"recovery precedes new-work permission gate");
    require([recordedState[@"pending_restart"] isEqual:fixtureState[@"pending_restart"]],"pending receipt preserved");

    reset(NO);fixtureState[@"pending_restart"]=@{@"phase":@"quit_requested"};
    require(invoke("--run")==0 && recoveryCalls==1,"pending quit recovery also retains dispatch");
    reset(NO);fixtureState[@"pending_restart"]=@{@"phase":@"launch_requested"};
    require(invoke("--dry-run")==0 && recoveryCalls==1 && recordedDry,"dry pending recovery retained");

    reset(NO);
    require(invoke("--dry-run")==0,"dry diagnosis available without permission");
    require(historyCalls==1 && censusCalls==1 && telemetryReads==1,"dry run keeps full diagnosis");
    require(recordedDry && [recordedReason isEqual:@"fixture_active_work"] && quitCalls==0,"dry diagnosis no action");

    reset(YES);
    require(invoke("--run")==0 && historyCalls==1 && censusCalls==1,"permission resumes existing guards");
    require([recordedReason isEqual:@"fixture_active_work"] && quitCalls==0,"active-work guard retained");

    reset(NO);fixtureValid=NO;
    require(invoke("--run")==2 && recordedReason==nil && historyCalls==0 && recoveryCalls==0,"configuration validation remains first");
    reset(NO);fixtureConfig=@{@"enabled":@NO};fixtureState[@"pending_restart"]=@{@"phase":@"launch_requested"};
    require(invoke("--run")==0 && recoveryCalls==1,"disabled configuration still routes owned recovery");
    printf("%d actual-entrypoint routing checks passed; all collaborators inert\n",checks);
    return 0;
} }
'''

with tempfile.TemporaryDirectory(prefix='codex-permission-routing-') as td:
    source = Path(td) / 'entrypoint.m'
    binary = Path(td) / 'entrypoint'
    source.write_text(prefix + '\n' + entry + '\n' + suffix)
    subprocess.run(['/usr/bin/clang', '-fobjc-arc', '-O0', '-Wall', '-Wextra', '-Werror',
                    '-framework', 'Foundation', str(source), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
