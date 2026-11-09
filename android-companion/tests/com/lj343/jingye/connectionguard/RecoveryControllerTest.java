package com.lj343.jingye.connectionguard;

import java.util.ArrayList;
import java.util.List;

/** Adversarial event/clock/recreation tests of the actual policy, no Android or network. */
public final class RecoveryControllerTest {
    private static int tests;
    private static final class Rig implements RecoveryController.Port, RecoveryController.Scheduler {
        boolean wifi=true, permission=true, accept=true, latch=true, wake, throwWrite, nextReadOff;
        int flag, writes, failSaveAtAttempt=-1;
        long elapsed, wallBase=1_000_000, due;
        Runnable queued;
        RecoveryController.Snapshot last;
        final List<Runnable> captured=new ArrayList<>();
        final RecoveryController c;
        Rig(){this(0,new long[0],0,0);}
        Rig(long attempts,long[] times,int used,long next){c=new RecoveryController(this,this,attempts,times,used,next);}
        public boolean wifiAvailable(){return wifi;}
        public int readEnabled(){if(nextReadOff){nextReadOff=false;return 0;}return flag;}
        public boolean canWrite(){return permission;}
        public long nowMillis(){return wallBase+elapsed;}
        public boolean enableOnce(){
            check(last!=null && last.attemptConsumed && last.attempts==c.getState().attempts,"budget persisted before write");
            writes++;if(throwWrite)throw new IllegalStateException("SYNTHETIC_WRITE_FAULT");
            if(accept && latch)flag=1;return accept;
        }
        public void holdWake(long ms){check(ms<=25_000,"bounded wake");wake=true;}
        public void releaseWake(){wake=false;}
        public void changed(RecoveryController.Snapshot s){
            if(s.attempts==failSaveAtAttempt)throw new IllegalStateException("SYNTHETIC_PERSIST_FAULT");
            last=s;
        }
        public void schedule(Runnable task,long delay){check(queued==null,"at most one timer");check(delay>=0,"nonnegative delay");queued=task;due=elapsed+delay;captured.add(task);}
        public void cancel(Runnable task){if(queued==task)queued=null;}
        void advance(long ms){long end=elapsed+ms;int calls=0;while(queued!=null && due<=end){check(++calls<200,"no timer spin");Runnable task=queued;queued=null;elapsed=due;task.run();}elapsed=end;}
        void set(int value){flag=value;c.settingChanged();}
        void net(boolean value){wifi=value;c.networkChanged(value);}
        void earlyReset(){c.start(false);advance(4_000);set(0);}
        Rig recreate(){RecoveryController.Snapshot s=c.getState();c.dispose();Rig r=new Rig(s.attempts,s.recentAttempts,s.autoRetriesUsed,s.retryNotBeforeMs);r.wallBase=nowMillis();r.flag=flag;r.wifi=wifi;r.permission=permission;r.latch=latch;r.c.start(s.attemptConsumed);return r;}
    }
    private static void check(boolean ok,String why){if(!ok)throw new AssertionError(why);}
    private static void phase(Rig r,String p){check(p.equals(r.c.getState().phase),"expected "+p+", got "+r.c.getState().phase);}
    private static void test(String name,Runnable action){action.run();tests++;System.out.println("PASS "+name);}
    public static void main(String[] args){
        test("default and unsolicited callbacks cannot write",()->{
            Rig r=new Rig();for(int i=0;i<100;i++){r.set(0);r.net(true);}r.advance(86_400_000);
            phase(r,"STOPPED");check(r.writes==0 && r.queued==null && !r.wake,"inert");
        });
        test("healthy already-on operation never schedules recovery",()->{
            Rig r=new Rig();r.flag=1;r.c.start(false);for(int i=0;i<100;i++){r.set(1);r.net(true);}r.advance(86_400_000);
            phase(r,"OPEN_PENDING_PC");check(r.writes==0 && r.queued==null && !r.wake,"healthy quiet");
        });
        test("initial four-second stabilization and full verification retained",()->{
            Rig r=new Rig();r.c.start(false);r.advance(3_999);check(r.writes==0,"not early");r.advance(1);
            phase(r,"VERIFYING");r.advance(12_000);phase(r,"COOLDOWN");check(!r.wake,"verification releases wake");
            r.advance(17_999);check(r.c.getState().attemptConsumed,"not rearmed early");r.advance(1);phase(r,"OPEN_PENDING_PC");
        });
        test("morning immediate reset recovers automatically after sixty seconds",()->{
            Rig r=new Rig();r.earlyReset();phase(r,"RETRY_WAIT");check(!r.wake && r.c.getState().timerPending,"quiet bounded retry planned");
            r.advance(59_999);check(r.writes==1,"no early retry");r.advance(1);phase(r,"RETRY_STABILIZING");
            r.advance(3_999);check(r.writes==1,"retry restabilizes");r.advance(1);check(r.writes==2 && r.c.getState().autoRetriesUsed==1,"second attempt");
            r.advance(30_000);phase(r,"OPEN_PENDING_PC");check(!r.c.getState().attemptConsumed && r.c.getState().autoRetriesUsed==0,"verified recovery resets episode");
        });
        test("never-latching setting gets two backed-off retries then explicit exhaustion",()->{
            Rig r=new Rig();r.latch=false;r.c.start(false);r.advance(16_000);phase(r,"RETRY_WAIT");
            r.advance(63_999);check(r.writes==1,"first retry not early");r.advance(1);check(r.writes==2,"first retry due");
            r.advance(12_000);phase(r,"RETRY_WAIT");r.advance(123_999);check(r.writes==2,"second backoff retained");r.advance(1);check(r.writes==3,"last allowed retry");
            r.advance(12_000);phase(r,"WAIT_EXTERNAL_ON");check("RETRY_EXHAUSTED".equals(r.c.getState().code),"visible reason");
            r.advance(86_400_000);check(r.writes==3 && r.queued==null && !r.wake,"no unlimited loop after expiry");
        });
        test("third attempt may recover inside the existing five-minute task budget",()->{
            Rig r=new Rig();r.latch=false;r.c.start(false);r.advance(92_000);check(r.writes==2,"two failed attempts");
            r.latch=true;r.advance(154_000);phase(r,"OPEN_PENDING_PC");check(r.writes==3 && r.elapsed<300_000,"third recovery within 300s");
        });
        test("duplicate OFF and network callbacks neither replenish nor postpone retries",()->{
            Rig r=new Rig();r.earlyReset();long at=r.c.getState().retryNotBeforeMs;Runnable timer=r.queued;
            for(int i=0;i<59;i++){r.advance(1_000);r.set(0);r.net(true);}
            check(r.queued==timer && r.c.getState().retryNotBeforeMs==at,"same original deadline");
            r.advance(1_000);for(int i=0;i<100;i++){r.set(0);r.net(true);}r.advance(4_000);check(r.writes==2,"one retry only");
        });
        test("ordinary network loss before writing waits for a new stable window",()->{
            Rig r=new Rig();r.c.start(false);r.advance(2_000);r.net(false);r.advance(100_000);check(r.writes==0 && !r.wake,"offline quiet");
            r.net(true);r.advance(3_999);check(r.writes==0,"full stability");r.advance(1);check(r.writes==1,"one write after return");
        });
        test("network loss during verification cannot create a fresh episode budget",()->{
            Rig r=new Rig();r.c.start(false);r.advance(4_000);r.flag=0;r.net(false);long at=r.c.getState().retryNotBeforeMs;
            r.advance(80_000);check(r.writes==1 && !r.wake,"no Wi-Fi no writes");r.net(true);r.advance(4_000);
            check(r.writes==2 && r.c.getState().autoRetriesUsed==1 && at>0,"same episode on return");
        });
        test("repeated Wi-Fi loss cannot reset or exhaust the pending budget",()->{
            Rig r=new Rig();r.earlyReset();long at=r.c.getState().retryNotBeforeMs;
            for(int i=0;i<20;i++){r.net(false);r.advance(100);r.net(true);r.advance(100);}
            check(r.writes==1 && r.c.getState().autoRetriesUsed==0 && r.c.getState().retryNotBeforeMs==at,"budget unchanged");
            r.advance(60_000);check(r.writes==2,"retry not stranded");
        });
        test("retry write deadline rechecks actual Wi-Fi availability",()->{
            Rig r=new Rig();r.earlyReset();r.advance(60_000);r.wifi=false;r.advance(4_000);
            phase(r,"WAIT_WIFI_RETRY");check(r.writes==1 && !r.wake,"deadline fail closed");r.net(true);r.advance(4_000);check(r.writes==2,"return recovers");
        });
        test("external ON before retry cancels writing and requires thirty stable seconds",()->{
            Rig r=new Rig();r.earlyReset();r.advance(10_000);r.set(1);phase(r,"EXTERNAL_ON_STABILIZING");
            r.advance(29_999);check(r.c.getState().attemptConsumed && r.writes==1,"no early rearm");r.advance(1);
            phase(r,"OPEN_PENDING_PC");r.advance(100_000);check(r.writes==1 && r.queued==null,"cancelled retry stays cancelled");
        });
        test("external OFF then ON resets only the stability timer",()->{
            Rig r=new Rig();r.earlyReset();long at=r.c.getState().retryNotBeforeMs;r.set(1);r.advance(20_000);r.set(0);
            check(r.c.getState().retryNotBeforeMs==at,"backoff not extended");r.set(1);r.advance(29_999);check(r.c.getState().attemptConsumed,"new complete stable interval");
            r.advance(1);phase(r,"OPEN_PENDING_PC");check(r.writes==1,"no extra write");
        });
        test("observed OFF cannot be erased by a subsequent ON read",()->{
            Rig r=new Rig();r.flag=1;r.c.start(true);r.advance(29_999);r.nextReadOff=true;r.c.settingChanged();
            check(!"EXTERNAL_ON_STABILIZING".equals(r.c.getState().phase),"observed off cancels old stable interval");
            r.c.settingChanged();r.advance(29_999);check(r.c.getState().attemptConsumed,"fresh interval");r.advance(1);check(r.writes==0,"no unnecessary write");
        });
        test("OFF then ON at stability deadline does not leave a timerless phase",()->{
            Rig r=new Rig();r.flag=1;r.c.start(true);r.nextReadOff=true;r.advance(30_000);
            phase(r,"EXTERNAL_ON_STABILIZING");check(r.queued!=null,"fresh timer exists");r.advance(30_000);phase(r,"OPEN_PENDING_PC");
        });
        test("duplicate external ON does not postpone stable recovery",()->{
            Rig r=new Rig();r.flag=1;r.c.start(true);Runnable timer=r.queued;
            for(int i=0;i<29;i++){r.advance(1_000);r.set(1);r.net(true);}check(r.queued==timer,"one stability timer");r.advance(1_000);phase(r,"OPEN_PENDING_PC");
        });
        test("network OFF observation cancels stability despite an already restored read",()->{
            Rig r=new Rig();r.flag=1;r.c.start(true);r.advance(29_999);r.c.networkChanged(false);
            phase(r,"WAIT_WIFI_RETRY");r.c.networkChanged(true);r.advance(29_999);check(r.c.getState().attemptConsumed,"new full interval");r.advance(1);phase(r,"OPEN_PENDING_PC");
        });
        test("consumed-on service recreation remains read-only during stability",()->{
            Rig r=new Rig(7,new long[]{999_000},1,1_060_000);r.flag=1;r.c.start(true);r.advance(29_999);check(r.writes==0 && r.c.getState().attemptConsumed,"not early");
            r.advance(1);phase(r,"OPEN_PENDING_PC");check(r.c.getState().autoRetriesUsed==0 && r.c.getState().retryNotBeforeMs==0,"episode clears after stability");
        });
        test("recreation retains absolute deadline and consumed count",()->{
            Rig r=new Rig();r.earlyReset();long at=r.c.getState().retryNotBeforeMs;r.advance(20_000);Rig restored=r.recreate();
            check(restored.c.getState().retryNotBeforeMs==at && restored.c.getState().attempts==1,"persisted episode restored");
            restored.advance(43_999);check(restored.writes==0,"not early after recreation");restored.advance(1);check(restored.writes==1 && restored.c.getState().autoRetriesUsed==1,"remaining time honored");
        });
        test("repeated recreation cannot postpone the original retry forever",()->{
            Rig r=new Rig();r.earlyReset();long at=r.c.getState().retryNotBeforeMs;
            for(int i=0;i<5;i++){r.advance(10_000);r=r.recreate();check(r.c.getState().retryNotBeforeMs==at,"deadline unchanged");}
            r.advance(14_000);check(r.writes==1 && r.c.getState().attempts==2,"retry still occurs");
        });
        test("recreation after a used retry cannot grant two new retries",()->{
            Rig r=new Rig();r.latch=false;r.c.start(false);r.advance(92_000);check(r.c.getState().autoRetriesUsed==1,"first retry consumed");
            Rig restored=r.recreate();restored.advance(136_000);check(restored.writes==1,"one remaining write");
            restored.advance(1_000_000);check(restored.writes==1 && "RETRY_EXHAUSTED".equals(restored.c.getState().code),"no new budget");
        });
        test("recreation of exhausted episode cannot write after rate window expires",()->{
            Rig r=new Rig(3,new long[]{1,2,3},2,0);r.c.start(true);r.advance(86_400_000);
            check(r.writes==0 && "RETRY_EXHAUSTED".equals(r.c.getState().code),"exhaustion persists");
        });
        test("old 0.3 consumed record migrates to bounded retry without immediate write",()->{
            Rig r=new Rig(13,new long[]{900_000},0,0);r.c.start(true);phase(r,"RETRY_WAIT");r.advance(63_999);check(r.writes==0,"migration honors delay");
            r.advance(1);check(r.writes==1 && r.c.getState().attempts==14,"migration resumes automatically");
        });
        test("global ten-minute circuit limits automatic retries",()->{
            Rig r=new Rig(2,new long[]{999_000,999_500},0,0);r.earlyReset();r.advance(64_000);phase(r,"PAUSED");
            check(r.writes==1 && "RATE_LIMITED".equals(r.c.getState().code) && !r.wake,"fourth write prevented");
            r.set(1);r.advance(1_000_000);check(!r.c.getState().armed,"rate hard stop not auto-revived");
        });
        test("manual repeated starts cannot bypass persisted global circuit",()->{
            Rig r=new Rig();r.latch=false;for(int i=0;i<3;i++){r.c.start(false);r.advance(16_000);}r.c.start(false);r.advance(4_000);
            phase(r,"PAUSED");check(r.writes==3 && "RATE_LIMITED".equals(r.c.getState().code),"manual reset does not erase rate history");
        });
        test("permission revoked during retry wait hard-stops without another write",()->{
            Rig r=new Rig();r.earlyReset();r.permission=false;r.advance(60_000);phase(r,"PAUSED");check(r.writes==1,"no retry without permission");
            r.permission=true;r.set(1);r.net(true);r.advance(1_000_000);check(!r.c.getState().armed && r.writes==1,"no silent revival");
        });
        test("permission revoked at retry stability deadline is rechecked",()->{
            Rig r=new Rig();r.earlyReset();r.advance(60_000);r.permission=false;r.advance(4_000);phase(r,"PAUSED");check(r.writes==1,"no unauthorized write");
        });
        test("initial setting rejection remains a hard stop",()->{
            Rig r=new Rig();r.accept=false;r.c.start(false);r.advance(4_000);phase(r,"PAUSED");r.set(1);r.advance(1_000_000);
            check(r.writes==1 && !r.c.getState().armed && "SETTING_WRITE_REJECTED".equals(r.c.getState().code),"no rejected-setting retry");
        });
        test("setting rejection during an automatic retry also hard-stops",()->{
            Rig r=new Rig();r.earlyReset();r.accept=false;r.advance(64_000);phase(r,"PAUSED");r.advance(1_000_000);check(r.writes==2,"exactly one rejected retry");
        });
        test("storage failure before a retry prevents its setting write",()->{
            Rig r=new Rig();r.earlyReset();r.failSaveAtAttempt=2;
            boolean ok=FailureBoundary.run(()->r.advance(64_000),r.c::dispose);
            check(!ok && r.writes==1 && !r.c.getState().armed && r.queued==null && !r.wake,"persist before write fail closed");
        });
        test("platform exception at retry does not loop or revive",()->{
            Rig r=new Rig();r.earlyReset();r.throwWrite=true;boolean ok=FailureBoundary.run(()->r.advance(64_000),r.c::dispose);
            check(!ok && r.writes==2 && !r.c.getState().armed && !r.wake,"platform failure disposed");r.set(1);r.net(true);r.advance(1_000_000);check(r.writes==2,"no retry after platform failure");
        });
        test("STOP invalidates even already dispatched retry callbacks",()->{
            Rig r=new Rig();r.earlyReset();Runnable stale=r.queued;r.c.stop();stale.run();r.net(false);r.net(true);r.set(0);r.advance(1_000_000);
            phase(r,"STOPPED");check(r.writes==1 && r.queued==null && !r.wake,"user stop wins");
        });
        test("STOP during retry stabilization invalidates the write callback",()->{
            Rig r=new Rig();r.earlyReset();r.advance(60_000);Runnable stale=r.queued;r.c.stop();stale.run();r.advance(1_000_000);check(r.writes==1 && !r.wake,"no stale write");
        });
        test("old generation cannot fire after a new explicit start",()->{
            Rig r=new Rig();r.earlyReset();Runnable stale=r.queued;r.c.stop();r.c.start(false);stale.run();r.advance(4_000);check(r.writes==2,"new arm only");
        });
        test("backward wall clock never skips backoff",()->{
            Rig r=new Rig();r.earlyReset();r.wallBase-=30_000;r.advance(60_000);check(r.writes==1 && !r.wake,"clock rollback not early");
            r.advance(33_999);check(r.writes==1,"remaining deadline honored");r.advance(1);check(r.writes==2,"eventual bounded retry");
        });
        test("forward clock cannot multiply pending retries",()->{
            Rig r=new Rig();r.earlyReset();r.wallBase+=3_600_000;for(int i=0;i<100;i++)r.set(0);
            r.advance(64_000);check(r.writes==2,"one callback only");r.advance(30_000);phase(r,"OPEN_PENDING_PC");
        });
        test("retry exhaustion can accept genuine external stable ON without a write",()->{
            Rig r=new Rig(3,new long[]{999_000,999_100,999_200},2,0);r.c.start(true);r.set(1);r.advance(30_000);phase(r,"OPEN_PENDING_PC");
            check(r.writes==0,"external recovery is read only");r.set(0);r.advance(4_000);phase(r,"PAUSED");check(r.writes==0,"old rate history retained");
        });
        test("stable recovered network supports a later independent episode",()->{
            Rig r=new Rig();r.earlyReset();r.advance(94_000);phase(r,"OPEN_PENDING_PC");r.net(false);r.flag=0;r.net(true);r.advance(34_000);
            phase(r,"OPEN_PENDING_PC");check(r.writes==3 && r.c.getState().autoRetriesUsed==0,"independent recovery retained");
        });
        test("invalid persisted retry records fail closed",()->{
            for(int used:new int[]{-1,3,Integer.MAX_VALUE}){boolean failed=false;try{new Rig(0,new long[0],used,0);}catch(IllegalArgumentException expected){failed=true;}check(failed,"bad count rejected");}
            boolean failed=false;try{new Rig(0,new long[0],0,-1);}catch(IllegalArgumentException expected){failed=true;}check(failed,"bad deadline rejected");
        });
        test("corrupt historical rate records cannot enable retry",()->{
            Rig r=new Rig(3,new long[]{Long.MAX_VALUE,Long.MAX_VALUE,Long.MAX_VALUE},0,0);r.c.start(true);r.advance(64_000);
            phase(r,"PAUSED");check(r.writes==0 && "RATE_LIMITED".equals(r.c.getState().code),"corrupt rate history stays conservative");
        });
        test("best-effort boundary cleanup still runs every action",()->{
            int[] n={0};boolean ok=FailureBoundary.cleanup(()->{n[0]++;throw new IllegalStateException();},()->n[0]++,()->n[0]++);
            check(!ok && n[0]==3,"independent cleanup attempts");
        });
        System.out.println("ALL "+tests+" TESTS PASSED");
    }
}
