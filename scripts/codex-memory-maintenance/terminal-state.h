#ifndef CMM_TERMINAL_STATE_H
#define CMM_TERMINAL_STATE_H
#include <stdbool.h>
#include <stdint.h>
#include <string.h>
#include <sys/types.h>
#include <sys/proc_info.h>

// proc_bsdinfo.pbi_status is a BSD process lifecycle state, not the Mach
// thread run state. Even a parked zsh can remain SRUN there. Require a complete,
// stable libproc thread census in TH_STATE_WAITING instead; fail closed on races.
typedef int (*CMMProcInfoQuery)(int, int, uint64_t, void *, int);
static bool CMMTerminalWaitingSnapshot(pid_t pid, uint64_t expectedStart,
                                      struct proc_taskinfo *result,
                                      CMMProcInfoQuery query) {
    struct proc_bsdinfo b = {0}; struct proc_taskinfo before = {0}, after = {0};
    uint64_t threads[65] = {0}, again[65] = {0};
    if (query(pid, PROC_PIDTBSDINFO, 0, &b, sizeof b) != sizeof b ||
        b.pbi_start_tvsec * 1000000ULL + b.pbi_start_tvusec != expectedStart ||
        query(pid, PROC_PIDTASKINFO, 0, &before, sizeof before) != sizeof before ||
        before.pti_threadnum < 1 || before.pti_threadnum > 64 || before.pti_numrunning != 0)
        return false;
    int bytes = query(pid, PROC_PIDLISTTHREADS, 0, threads, sizeof threads);
    if (bytes != before.pti_threadnum * (int)sizeof(uint64_t)) return false;
    for (int i = 0; i < before.pti_threadnum; i++) {
        if (!threads[i]) return false;
        for (int j = 0; j < i; j++) if (threads[i] == threads[j]) return false;
        struct proc_threadinfo thread = {0};
        if (query(pid, PROC_PIDTHREADINFO, threads[i], &thread, sizeof thread) != sizeof thread ||
            thread.pth_run_state != TH_STATE_WAITING) return false;
    }
    if (query(pid, PROC_PIDLISTTHREADS, 0, again, sizeof again) != bytes ||
        memcmp(threads, again, (size_t)bytes) != 0 ||
        query(pid, PROC_PIDTASKINFO, 0, &after, sizeof after) != sizeof after ||
        after.pti_threadnum != before.pti_threadnum || after.pti_numrunning != 0 ||
        after.pti_total_user != before.pti_total_user || after.pti_total_system != before.pti_total_system ||
        query(pid, PROC_PIDTBSDINFO, 0, &b, sizeof b) != sizeof b ||
        b.pbi_start_tvsec * 1000000ULL + b.pbi_start_tvusec != expectedStart)
        return false;
    *result = after;
    return true;
}
#endif
