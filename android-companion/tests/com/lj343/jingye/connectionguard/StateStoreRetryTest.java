package com.lj343.jingye.connectionguard;
import android.content.Context;
import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;

/** Exercises the actual StateStore using a deterministic private-preferences test double. */
public final class StateStoreRetryTest {
    static int count;
    static class Prefs implements SharedPreferences {
        final Map<String,Object> values=new HashMap<>(); boolean fail,explode;
        public boolean getBoolean(String k,boolean d){return values.containsKey(k)?(Boolean)values.get(k):d;}
        public long getLong(String k,long d){return values.containsKey(k)?(Long)values.get(k):d;}
        public String getString(String k,String d){return values.containsKey(k)?(String)values.get(k):d;}
        public Editor edit(){return new Editor(){
            final Map<String,Object> pending=new HashMap<>();
            public Editor putBoolean(String k,boolean v){pending.put(k,v);return this;}
            public Editor putLong(String k,long v){pending.put(k,v);return this;}
            public Editor putString(String k,String v){pending.put(k,v);return this;}
            public boolean commit(){if(explode)throw new IllegalStateException("SYNTHETIC_COMMIT");if(fail)return false;values.putAll(pending);return true;}
        };}
    }
    static class Ctx extends Context {
        final Prefs prefs=new Prefs();
        public SharedPreferences getSharedPreferences(String name,int mode){check("guard_state".equals(name),"private state only");return prefs;}
    }
    static void check(boolean ok,String text){if(!ok)throw new AssertionError(text);}
    static void test(String name,Runnable task){task.run();count++;System.out.println("PASS "+name);}
    static RecoveryController.Snapshot snapshot(int used,long at){return new RecoveryController.Snapshot(true,true,false,true,true,"RETRY_WAIT","WAIT_RETRY",16,new long[]{100,200},used,at);}
    public static void main(String[] args){
        test("old 0.3 preferences default retry fields without erasing history",()->{
            Ctx c=new Ctx();c.prefs.values.put("attempts",15L);c.prefs.values.put("attempt_times","100,200");StateStore store=new StateStore(c);
            check(store.autoRetriesUsed()==0 && store.retryNotBeforeMs()==0 && store.attempts()==15 && store.recentAttempts().length==2,"migration defaults");
        });
        test("actual StateStore persists count deadline and consumed budget together",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);check(store.save(snapshot(1,1234567),true),"save succeeds");
            StateStore recreated=new StateStore(c);check(recreated.autoRetriesUsed()==1 && recreated.retryNotBeforeMs()==1234567 && recreated.consumed() && recreated.desiredArmed(),"atomic state restored");
            check(recreated.attempts()==16 && recreated.recentAttempts().length==2,"global budget retained");
        });
        test("serviceGone does not clear retry deadline or rate history",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);store.save(snapshot(1,1234567),true);store.serviceGone();StateStore recreated=new StateStore(c);
            check(recreated.autoRetriesUsed()==1 && recreated.retryNotBeforeMs()==1234567 && recreated.consumed(),"episode survives teardown");
            check(!c.prefs.getBoolean("service_running",true) && !c.prefs.getBoolean("timer_pending",true),"runtime state marked absent");
        });
        test("failed commit leaves original retry budget intact",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);store.save(snapshot(1,1234567),true);c.prefs.fail=true;
            check(!store.save(snapshot(2,0),true),"failure returned to write gate");check(store.autoRetriesUsed()==1 && store.retryNotBeforeMs()==1234567,"previous record unchanged");
        });
        test("commit exception remains a platform failure",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);c.prefs.explode=true;boolean threw=false;try{store.save(snapshot(1,123),true);}catch(IllegalStateException expected){threw=true;}check(threw,"exception not swallowed");
        });
        test("platform failure revokes recovery intent and pending retry",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);store.save(snapshot(1,1234567),true);check(store.platformFailure(),"failure stored");
            check(!store.desiredArmed() && store.retryNotBeforeMs()==0 && !c.prefs.getBoolean("timer_pending",true),"no retry after platform fault");
        });
        test("corrupt retry count or deadline cannot grant new writes",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);c.prefs.values.put("auto_retries_used",3L);boolean failed=false;try{store.autoRetriesUsed();}catch(IllegalStateException expected){failed=true;}check(failed,"bad count rejected");
            c.prefs.values.put("auto_retries_used",0L);c.prefs.values.put("retry_not_before_ms",-1L);failed=false;try{store.retryNotBeforeMs();}catch(IllegalStateException expected){failed=true;}check(failed,"bad deadline rejected");
        });
        test("diagnostics expose retry status without configuration secrets",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);store.save(snapshot(1,1234567),true);String json=store.snapshot();
            check(json.contains("\"auto_retries_used\":1") && json.contains("\"next_retry_at_ms\":1234567") && json.contains("\"auto_retry_limit\":2"),"retry fields visible");
            check(!json.contains("shared_key") && !json.contains("reporter_id") && !json.contains("listen_ipv4"),"no report provisioning secrets");
        });
        test("history remains bounded and includes changed retry deadline",()->{
            Ctx c=new Ctx();StateStore store=new StateStore(c);for(int i=0;i<100;i++)store.save(snapshot(1,1234500+i),true);
            check(store.history().split("\n").length==40 && store.history().contains("next_retry=1234599"),"forty recent transitions only");
        });
        System.out.println("ALL "+count+" STORAGE TESTS PASSED");
    }
}
