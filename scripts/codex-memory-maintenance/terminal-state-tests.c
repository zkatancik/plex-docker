#include "terminal-state.h"
#include <stdio.h>
#include <stdlib.h>
static int mode, tasks, lists, bsds, checks;
static int fake(int pid, int flavor, uint64_t arg, void *buffer, int bytes) {
    (void)pid; (void)arg; (void)bytes;
    if(mode==1)return 0;
    if(flavor==PROC_PIDTBSDINFO) { struct proc_bsdinfo *b=buffer;memset(b,0,sizeof *b);b->pbi_start_tvsec=10;b->pbi_status=2;if(mode==12 && bsds++)b->pbi_start_tvsec=11;else if(mode!=12)bsds++;return sizeof *b; }
    if(flavor==PROC_PIDTASKINFO) { struct proc_taskinfo *t=buffer;memset(t,0,sizeof *t);t->pti_threadnum=mode==2?0:(mode==3?65:((mode==13||mode==14||mode==15)?2:1));t->pti_numrunning=mode==4?1:0;t->pti_total_user=4;if(mode==10 && tasks)t->pti_total_user++;if(mode==11 && tasks)t->pti_numrunning=1;tasks++;return sizeof *t; }
    if(flavor==PROC_PIDLISTTHREADS) { ((uint64_t *)buffer)[0]=(mode==9 && lists)?2:1;((uint64_t *)buffer)[1]=mode==15?1:2;lists++;return mode==5?4:((mode==13||mode==14||mode==15)?16:8); }
    if(flavor==PROC_PIDTHREADINFO) { struct proc_threadinfo *t=buffer;memset(t,0,sizeof *t);t->pth_run_state=(mode==6||(mode==14&&arg==2))?TH_STATE_RUNNING:(mode==7?TH_STATE_UNINTERRUPTIBLE:(mode==16?TH_STATE_STOPPED:(mode==17?0:TH_STATE_WAITING)));return mode==8?0:sizeof *t; }
    return 0;
}
int main(void) {
    for(int m=0;m<=17;m++) { mode=m;tasks=lists=bsds=0;struct proc_taskinfo t={0};
        bool value=CMMTerminalWaitingSnapshot(123,10000000,&t,fake);
        if(value!=(m==0||m==13)) {fprintf(stderr,"failed mode%d\n",m);return 1;} checks++;
    }
    mode=0;tasks=lists=bsds=0;struct proc_taskinfo t={0};
    if(CMMTerminalWaitingSnapshot(123,9999999,&t,fake))return 1;checks++;
    printf("%d terminal wait-state tests passed\n",checks);return 0;
}
