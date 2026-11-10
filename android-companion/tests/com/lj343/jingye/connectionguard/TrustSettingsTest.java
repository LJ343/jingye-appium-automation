package com.lj343.jingye.connectionguard;

import android.content.Context;
import android.content.SharedPreferences;
import java.util.HashMap;
import java.util.Map;

/** Tests actual production preferences adapter; no Android or device actions. */
public final class TrustSettingsTest {
    static int count;
    static final TrustIdentity AP=TrustIdentity.of("Private test network","12:34:56:78:9a:bc");
    static class Prefs implements SharedPreferences {
        final Map<String,Object> values=new HashMap<>();boolean fail,explode,applyMemoryOnFailure;
        public boolean getBoolean(String k,boolean d){return values.containsKey(k)?(Boolean)values.get(k):d;}
        public long getLong(String k,long d){return values.containsKey(k)?(Long)values.get(k):d;}
        public String getString(String k,String d){return values.containsKey(k)?(String)values.get(k):d;}
        public Editor edit(){return new Editor(){
            final Map<String,Object> pending=new HashMap<>();
            public Editor putBoolean(String k,boolean v){pending.put(k,v);return this;}
            public Editor putLong(String k,long v){pending.put(k,v);return this;}
            public Editor putString(String k,String v){pending.put(k,v);return this;}
            public boolean commit(){if(explode)throw new IllegalStateException("COMMIT_FAULT");if(!fail || applyMemoryOnFailure)values.putAll(pending);return !fail;}
        };}
    }
    static class Ctx extends Context {
        final Prefs prefs=new Prefs();
        public SharedPreferences getSharedPreferences(String n,int mode){check(n.equals("network_trust_v1") && mode==0,"private preferences only");return prefs;}
    }
    static void check(boolean x,String why){if(!x)throw new AssertionError(why);}
    static void test(String name,Runnable work){
        try{java.lang.reflect.Field f=TrustSettings.class.getDeclaredField("storageFault");f.setAccessible(true);f.setBoolean(null,false);}catch(Exception e){throw new AssertionError(e);}
        work.run();count++;System.out.println("PASS "+name);
    }
    static TrustSettings armed(Ctx c){TrustSettings s=new TrustSettings(c);check(s.add(AP.key)&&s.enable(true),"configured explicitly");return s;}
    public static void main(String[] args){
        test("upgrade defaults disabled and empty",()->{TrustSettings s=new TrustSettings(new Ctx());check(!s.enabled()&&s.count()==0&&!s.enable(true),"no inherited trust");});
        test("only AP digest persists and duplicate registration is idempotent",()->{Ctx c=new Ctx();TrustSettings s=armed(c);check(s.add(AP.key)&&s.count()==1,"deduplicated");check(!c.prefs.values.toString().contains(AP.ssid)&&!c.prefs.values.toString().contains(AP.bssid),"no plaintext AP");});
        test("allowlist limit refuses seventeenth AP",()->{Ctx c=new Ctx();TrustSettings s=new TrustSettings(c);for(int i=0;i<16;i++)check(s.add(TrustIdentity.of("Test",String.format("12:34:56:78:9a:%02x",i)).key),"within limit");check(!s.add(AP.key)&&s.count()==16,"bounded");});
        test("unknown AP cannot reserve",()->{TrustSettings s=armed(new Ctx());check(!s.reserve(TrustGuard.Action.ALLOW,"00".repeat(32),1),"not enrolled");});
        test("pause and resume do not refresh action allowance",()->{Ctx c=new Ctx();TrustSettings s=armed(c);check(s.reserve(TrustGuard.Action.ALLOW,AP.key,1),"first");check(s.enable(false)&&!s.reserve(TrustGuard.Action.ALLOW,AP.key,2),"paused");check(s.enable(true)&&!s.reserve(TrustGuard.Action.ALLOW,AP.key,1),"same dialog still consumed");});
        test("actual persistent ledger survives new store",()->{Ctx c=new Ctx();TrustSettings s=armed(c);check(s.reserve(TrustGuard.Action.ALLOW,AP.key,1),"first");check(!new TrustSettings(c).reserve(TrustGuard.Action.ALLOW,AP.key,1),"recreated same window");});
        test("clear does not erase budget and disables feature",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.reserve(TrustGuard.Action.ALLOW,AP.key,1);String old=c.prefs.getString("action_ledger","");check(s.clear()&&!s.enabled()&&s.count()==0,"cleared");check(old.equals(c.prefs.getString("action_ledger","")),"budget retained");});
        test("corrupt allowlist denies reads",()->{Ctx c=new Ctx();c.prefs.values.put("allowlist","*");boolean failed=false;try{new TrustSettings(c).contains(AP.key);}catch(IllegalStateException expected){failed=true;}check(failed,"not treated as wildcard");});
        test("diagnostic excludes hashes SSIDs and BSSIDs",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.reserve(TrustGuard.Action.ALLOW,AP.key,1);String d=s.diagnostic();check(d.contains("allowlisted_ap_count")&&!d.contains(AP.key)&&!d.contains(AP.ssid)&&!d.contains(AP.bssid),"counts only");});
        test("dialog closure status does not erase click evidence",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.record("ALLOW_CLICK_SENT_PC_UNVERIFIED");s.record("NO_MATCHING_DIALOG");check(c.prefs.getString("last_code","").equals("NO_MATCHING_DIALOG")&&c.prefs.getString("last_click_code","").equals("ALLOW_CLICK_SENT_PC_UNVERIFIED"),"state and action remain separate");});
        test("a new same-kind click gets a fresh action timestamp",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.record("ALLOW_CLICK_SENT_PC_UNVERIFIED");c.prefs.values.put("last_click_at_ms",1L);s.record("ALLOW_CLICK_SENT_PC_UNVERIFIED");check(c.prefs.getLong("last_click_at_ms",0)>1L,"not deduplicated as background state");});
        test("failed durable reserve denies UI even with in-memory preference change",()->{Ctx c=new Ctx();TrustSettings s=armed(c);c.prefs.fail=true;c.prefs.applyMemoryOnFailure=true;check(!s.reserve(TrustGuard.Action.ALLOW,AP.key,1)&&!s.enabled(),"latch closes gate");c.prefs.fail=false;check(!new TrustSettings(c).enabled(),"same-process recreation does not reopen");});
        test("exception during reserve propagates to service failure boundary",()->{Ctx c=new Ctx();TrustSettings s=armed(c);c.prefs.explode=true;boolean failed=false;try{s.reserve(TrustGuard.Action.ALLOW,AP.key,1);}catch(IllegalStateException expected){failed=true;}check(failed&&!s.enabled(),"no action after fault");});
        test("diagnostic write failure also closes authorization",()->{Ctx c=new Ctx();TrustSettings s=armed(c);c.prefs.fail=true;s.record("ALLOW_CLICK_SENT_PC_UNVERIFIED");check(!s.enabled(),"fail closed");});
        System.out.println("ALL "+count+" TRUST STORAGE TESTS PASSED");
    }
}
