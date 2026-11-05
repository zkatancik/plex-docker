// Reviewable fragment: vendor origin, not idle-work proof. No AX or execution.
// The caller must also qualify the node_repl parent's ancestry under its owned
// cua-repl host, reject unknown descendants, and retain the app's normal quit guard.
#import <Foundation/Foundation.h>
#import <Security/Security.h>
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>
#include <string.h>
#include <limits.h>

static NSString *CMMVHIdentity(struct stat s) {
    return [NSString stringWithFormat:@"%llu:%llu:%llu:%lld:%ld:%lld:%ld",
        (unsigned long long)s.st_dev, (unsigned long long)s.st_ino,
        (unsigned long long)s.st_size, (long long)s.st_mtimespec.tv_sec,
        s.st_mtimespec.tv_nsec, (long long)s.st_ctimespec.tv_sec, s.st_ctimespec.tv_nsec];
}
static int CMMVHOpen(NSString *path, BOOL script, struct stat *info) {
    if (![path isKindOfClass:NSString.class]) return -1;
    int fd = open(path.fileSystemRepresentation, O_RDONLY | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return -1;
    if (fstat(fd,info) || !S_ISREG(info->st_mode) || (info->st_mode & 0022) ||
        (script ? info->st_uid != getuid() || info->st_nlink != 1 :
                  info->st_uid != getuid() && info->st_uid != 0) ||
        info->st_size < 1 || info->st_size > (script ? 256*1024 : 512*1024*1024)) {
        close(fd); return -1;
    }
    return fd;
}
static BOOL CMMVHSigned(NSString *path, NSString *identifier) {
    struct stat before,after; int fd=CMMVHOpen(path,NO,&before);
    if(fd<0) return NO;
    close(fd);
    static NSMutableDictionary *cache;
    if(!cache) cache=[NSMutableDictionary dictionary];
    NSString *key=[NSString stringWithFormat:@"%@|%@|%@",path,identifier,CMMVHIdentity(before)];
    if([cache[key] boolValue]) return YES;
    SecStaticCodeRef code=NULL; SecRequirementRef requirement=NULL;
    // identifier is one of the two fixed call-site literals, never supplied by argv.
    NSString *rule=[NSString stringWithFormat:@"identifier \"%@\" and anchor apple generic and certificate leaf[subject.OU] = \"2DC432GLL2\"",identifier];
    if(SecRequirementCreateWithString((__bridge CFStringRef)rule,kSecCSDefaultFlags,&requirement)!=errSecSuccess) return NO;
    BOOL ok=SecStaticCodeCreateWithPath((__bridge CFURLRef)[NSURL fileURLWithPath:path],kSecCSDefaultFlags,&code)==errSecSuccess;
    if(ok) ok=SecStaticCodeCheckValidity(code,kSecCSCheckAllArchitectures|kSecCSStrictValidate,requirement)==errSecSuccess;
    if(code) CFRelease(code);
    CFRelease(requirement);
    fd=CMMVHOpen(path,NO,&after);
    ok=ok && fd>=0 && [CMMVHIdentity(before) isEqual:CMMVHIdentity(after)];
    if(fd>=0) close(fd);
    if(ok) { if(cache.count>=32) [cache removeAllObjects]; cache[key]=@YES; }
    return ok;
}
static BOOL CMMVHParent(NSString *parentPath, NSArray *parentArgv, NSString *bundle) {
    NSString *expected=[bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/bin/node_repl"];
    return [parentPath isEqual:expected] && [parentArgv isEqual:@[expected]] && CMMVHSigned(expected,@"node_repl");
}
static BOOL CMMVHTempScript(NSString *path) {
    char directory[PATH_MAX], temporary[PATH_MAX];
    NSString *parent=path.stringByDeletingLastPathComponent;
    struct stat s,rootStat;
    if(lstat(parent.fileSystemRepresentation,&s) || !S_ISDIR(s.st_mode) ||
       s.st_uid!=getuid() || (s.st_mode&0022) ||
       !realpath(parent.fileSystemRepresentation,directory) ||
       !realpath(NSTemporaryDirectory().fileSystemRepresentation,temporary) ||
       stat(temporary,&rootStat) || !S_ISDIR(rootStat.st_mode) ||
       rootStat.st_uid!=getuid() || (rootStat.st_mode&0077)) return NO;
    NSString *resolved=@(directory), *root=[@(temporary) stringByAppendingString:@"/"];
    return [resolved hasPrefix:root] && [resolved.lastPathComponent hasPrefix:@".tmp"];
}
static BOOL CMMVHBytesInBinary(NSString *script, NSString *binary) {
    struct stat sourceBefore,binaryBefore,sourceAfter,binaryAfter;
    int source=CMMVHOpen(script,YES,&sourceBefore);
    if(source<0) return NO;
    if(sourceBefore.st_size<1024) { close(source); return NO; }
    int image=CMMVHOpen(binary,NO,&binaryBefore);
    if(image<0) { close(source); return NO; }
    if(binaryBefore.st_size>128*1024*1024) {close(source);close(image);return NO;}
    NSString *key=[NSString stringWithFormat:@"%@|%@|%@|%@",script,CMMVHIdentity(sourceBefore),binary,CMMVHIdentity(binaryBefore)];
    static NSMutableDictionary *cache;
    if(!cache) cache=[NSMutableDictionary dictionary];
    if([cache[key] boolValue]) { close(source); close(image); return YES; }
    NSMutableData *needle=[NSMutableData dataWithLength:(NSUInteger)sourceBefore.st_size];
    size_t got=0; ssize_t n;
    while(got<needle.length && (n=read(source,(char *)needle.mutableBytes+got,needle.length-got))>0) got+=(size_t)n;
    BOOL found=NO,ok=got==needle.length;
    // At most 320 KiB for the scan plus the <=256 KiB source. No mmap or full
    // binary read; overlap ensures a match crossing a chunk boundary is found.
    NSMutableData *buffer=[NSMutableData dataWithLength:65536+needle.length];
    size_t retained=0;
    while(ok && (n=read(image,(char *)buffer.mutableBytes+retained,65536))>0) {
        size_t length=retained+(size_t)n;
        NSData *view=[NSData dataWithBytesNoCopy:buffer.mutableBytes length:length freeWhenDone:NO];
        if([view rangeOfData:needle options:0 range:NSMakeRange(0,length)].location!=NSNotFound) {found=YES;break;}
        retained=MIN(needle.length-1,length);
        memmove(buffer.mutableBytes,(char *)buffer.mutableBytes+length-retained,retained);
    }
    ok=ok && found && fstat(source,&sourceAfter)==0 && fstat(image,&binaryAfter)==0 &&
        [CMMVHIdentity(sourceBefore) isEqual:CMMVHIdentity(sourceAfter)] &&
        [CMMVHIdentity(binaryBefore) isEqual:CMMVHIdentity(binaryAfter)];
    close(source);close(image);
    if(ok) {if(cache.count>=32) [cache removeAllObjects];cache[key]=@YES;}
    return ok;
}
static BOOL CMMVHStrings(NSArray *argv) {
    if(![argv isKindOfClass:NSArray.class]) return NO;
    for(id a in argv) if(![a isKindOfClass:NSString.class]) return NO;
    return YES;
}
BOOL CMMVendorWorker(NSString *path, NSArray *argv, NSString *parentPath, NSArray *parentArgv, NSString *bundle) {
    NSString *node=[bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/bin/node"];
    if(![path isEqual:node] || !CMMVHStrings(argv) || !CMMVHParent(parentPath,parentArgv,bundle)) return NO;
    NSString *script=nil;
    if(argv.count==7 && [argv[0] isEqual:node] && [argv[1] isEqual:@"--experimental-vm-modules"] &&
       [argv[3] isEqual:@"--session-id"] && [argv[5] isEqual:@"--working-dir"] && [argv[6] isAbsolutePath]) {
        NSString *session=argv[4];
        NSCharacterSet *hex=[NSCharacterSet characterSetWithCharactersInString:@"0123456789abcdefABCDEF"];
        if(session.length!=32 || [session rangeOfCharacterFromSet:hex.invertedSet].location!=NSNotFound) return NO;
        script=argv[2];if(![script.lastPathComponent isEqual:@"kernel.js"]) return NO;
    } else if(argv.count==3 && [argv[0] isEqual:node] && [argv[2] isAbsolutePath]) {
        script=argv[1];if(![script.lastPathComponent isEqual:@"trusted-worker.js"]) return NO;
    } else return NO;
    return [script isAbsolutePath] && CMMVHTempScript(script) && CMMVHBytesInBinary(script,parentPath) &&
        CMMVHSigned(parentPath,@"node_repl");
}
BOOL CMMNestedStdioServer(NSString *path, NSArray *argv, NSString *parentPath, NSArray *parentArgv, NSString *bundle) {
    NSString *server=[bundle stringByAppendingPathComponent:@"Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"];
    return [path isEqual:server] && [argv isEqual:@[server,@"app-server",@"--listen",@"stdio://"]] &&
        CMMVHParent(parentPath,parentArgv,bundle) && CMMVHSigned(server,@"codex");
}
