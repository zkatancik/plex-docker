#import <Foundation/Foundation.h>

NSString *CMMPolicy(NSDictionary *config, NSArray *samples, NSDictionary *latest,
                    NSDictionary *state, double now, double monotonic,
                    double userIdle, long long bootTime);
NSString *CMMDialogDecision(NSArray<NSString *> *texts, NSArray<NSString *> *buttons,
                            BOOL isNewDialog, BOOL inputStillIdle);
BOOL CMMAllowedChild(NSString *path, NSString *bundle, BOOL directChild);
NSString *CMMProcessClass(NSString *path, NSArray *argv, NSString *cwd, NSString *bundle, NSString *home, BOOL directChild);
BOOL CMMUpdateTerminalQuiet(NSDictionary *snapshot, NSMutableDictionary *state, NSDictionary *config, double now, long long boot);
BOOL CMMVendorWorker(NSString *path, NSArray *argv, NSString *parentPath, NSArray *parentArgv, NSString *bundle);
BOOL CMMNestedStdioServer(NSString *path, NSArray *argv, NSString *parentPath, NSArray *parentArgv, NSString *bundle);
NSString *CMMRecoveryDecision(NSString *originalIdentity, NSUInteger instances,
                             BOOL replacementVerified, NSUInteger launchAttempts,
                             double sinceLastLaunch, NSUInteger maxAttempts, double retryDelay);
