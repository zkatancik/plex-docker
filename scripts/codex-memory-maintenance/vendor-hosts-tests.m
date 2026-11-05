#import "vendor-hosts.m"
#include <stdlib.h>
static int checks;
static void check(BOOL value, NSString *label) { checks++; if(!value){fprintf(stderr,"FAIL: %s\n",label.UTF8String);exit(1);} }
int main(void) { @autoreleasepool {
    NSString *base=[NSTemporaryDirectory() stringByAppendingPathComponent:@".tmp-cmm-vendor-XXXXXX"];
    char temporary[PATH_MAX];strlcpy(temporary,base.fileSystemRepresentation,sizeof(temporary));check(mkdtemp(temporary)!=NULL,@"private fixture directory");base=@(temporary);
    NSString *script=[base stringByAppendingPathComponent:@"kernel.js"],*binary=[base stringByAppendingPathComponent:@"fixture.bin"];
    NSMutableData *needle=[NSMutableData dataWithLength:2000];memset(needle.mutableBytes,'Q',needle.length);
    NSMutableData *bytes=[NSMutableData dataWithLength:65500];memset(bytes.mutableBytes,'X',bytes.length);[bytes appendData:needle];[bytes appendData:[@"suffix" dataUsingEncoding:NSUTF8StringEncoding]];
    [needle writeToFile:script atomically:NO];[bytes writeToFile:binary atomically:NO];chmod(script.fileSystemRepresentation,0600);chmod(binary.fileSystemRepresentation,0600);
    check(CMMVHTempScript(script),@"private actual temp directory");
    check(CMMVHBytesInBinary(script,binary),@"exact match crosses chunk boundary");
    ((char *)needle.mutableBytes)[1000]='Z';[needle writeToFile:script atomically:NO];
    check(!CMMVHBytesInBinary(script,binary),@"same inode content change invalidates cache");
    chmod(script.fileSystemRepresentation,0666);check(!CMMVHBytesInBinary(script,binary),@"writable script rejected");chmod(script.fileSystemRepresentation,0600);
    NSString *link=[base stringByAppendingPathComponent:@"link.js"];symlink(script.fileSystemRepresentation,link.fileSystemRepresentation);check(!CMMVHBytesInBinary(link,binary),@"symlink rejected");unlink(link.fileSystemRepresentation);
    NSString *fifo=[base stringByAppendingPathComponent:@"fifo.js"];mkfifo(fifo.fileSystemRepresentation,0600);check(!CMMVHBytesInBinary(fifo,binary),@"FIFO rejected without blocking");unlink(fifo.fileSystemRepresentation);
    NSString *bundle=@"/Applications/ChatGPT.app",*node=[bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/bin/node"],*parent=[bundle stringByAppendingPathComponent:@"Contents/Resources/cua_node/bin/node_repl"],*server=[bundle stringByAppendingPathComponent:@"Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"];
    check(!CMMVendorWorker(node,@[node,script,@"/tmp"],@"/bin/unknown",@[@"/bin/unknown"],bundle),@"foreign parent rejected");
    check(!CMMVendorWorker(node,@[node,script,@"/tmp",@"extra"],parent,@[parent],bundle),@"extra worker arguments rejected");
    check(!CMMVendorWorker(node,@[node,@"--experimental-vm-modules",script,@"--session-id",@"not-hex",@"--working-dir",@"/tmp"],parent,@[parent],bundle),@"invalid session shape rejected");
    check(!CMMNestedStdioServer(server,@[server,@"app-server",@"--listen",@"http://localhost"],parent,@[parent],bundle),@"non-stdio nested server rejected");
    check(CMMNestedStdioServer(server,@[server,@"app-server",@"--listen",@"stdio://"],parent,@[parent],bundle),@"current signed nested stdio identity accepted without execution");
    // Read-only qualification against the observed real vendor files. No worker
    // process, application, or Accessibility operation is ever launched here.
    NSString *observed=@"/var/folders/dg/tpkkvdyn5tjg8tltfz8mfj9h0000gn/T/.tmp6EYtGe";
    NSString *kernel=[observed stringByAppendingPathComponent:@"kernel.js"],*trusted=[observed stringByAppendingPathComponent:@"trusted-worker.js"];
    if([NSFileManager.defaultManager fileExistsAtPath:kernel]) {
        check(CMMVendorWorker(node,@[node,@"--experimental-vm-modules",kernel,@"--session-id",@"0123456789abcdef0123456789abcdef",@"--working-dir",@"/tmp"],parent,@[parent],bundle),@"observed kernel matches signed runtime");
        check(CMMVendorWorker(node,@[node,trusted,@"/tmp"],parent,@[parent],bundle),@"observed trusted worker matches signed runtime");
    }
    [NSFileManager.defaultManager removeItemAtPath:base error:nil];
    printf("%d vendor-origin checks passed; no worker or AX execution\n",checks);
    return 0;
} }
