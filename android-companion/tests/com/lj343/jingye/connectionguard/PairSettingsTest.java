package com.lj343.jingye.connectionguard;
import android.content.Context;
import android.content.SharedPreferences;
import static com.lj343.jingye.connectionguard.TrustSettingsTest.*;

/** Actual durable adapter: pinning, restarts, rate, revocation and write failures. */
public final class PairSettingsTest {
    static final String HOST="ab".repeat(32);static final long NOW=1_794_963_600_000L;
    static class PairCtx extends Context {
        final Prefs prefs=new Prefs();
        public SharedPreferences getSharedPreferences(String name,int mode){check(name.equals("pairing_recovery_v1")&&mode==0,"private store");return prefs;}
    }
    static void run(String name,Runnable work){
        try{java.lang.reflect.Field f=PairSettings.class.getDeclaredField("fault");f.setAccessible(true);f.setBoolean(null,false);}catch(Exception e){throw new AssertionError(e);}
        work.run();count++;System.out.println("PASS "+name);
    }
    static PairProtocol.Request request(int n){return new PairProtocol.Request("cd".repeat(16),String.format("%064x",n),HOST,NOW/1000+40);}
    static PairSettings configured(PairCtx c){PairSettings s=new PairSettings(c);check(s.pin(HOST)&&s.enable(true),"explicit pin and enable");return s;}
    public static void main(String[] args){count=0;
        run("new APK defaults disabled",()->check(!new PairSettings(new PairCtx()).enabled(),"no inherited authorization"));
        run("enable requires a pinned host",()->check(!new PairSettings(new PairCtx()).enable(true),"not an arbitrary PC"));
        run("pin does not implicitly enable",()->{PairSettings s=new PairSettings(new PairCtx());check(s.pin(HOST)&&!s.enabled(),"independent opt-in");});
        run("different key cannot silently replace original",()->{PairSettings s=configured(new PairCtx());check(!s.pin("ac".repeat(32))&&s.fingerprint().equals(HOST),"pinned");});
        run("same pin is idempotent",()->{PairSettings s=configured(new PairCtx());check(s.pin(HOST)&&s.enabled(),"upgrade retains state");});
        run("request nonce cannot replay after recreation",()->{PairCtx c=new PairCtx();check(configured(c).reserve(request(1),NOW)&&!new PairSettings(c).reserve(request(1),NOW+1),"persistent nonce");});
        run("two attempts total across recreation",()->{PairCtx c=new PairCtx();PairSettings s=configured(c);check(s.reserve(request(1),NOW)&&s.reserve(request(2),NOW+1)&&!new PairSettings(c).reserve(request(3),NOW+2),"persistent limit");});
        run("disable and enable preserve attempt budget",()->{PairCtx c=new PairCtx();PairSettings s=configured(c);s.reserve(request(1),NOW);s.reserve(request(2),NOW+1);s.enable(false);s.enable(true);check(!s.reserve(request(3),NOW+2),"not replenished");});
        run("expired request cannot use UI allowance",()->check(!configured(new PairCtx()).reserve(request(1),NOW+41_000),"expired"));
        run("clock reversal fails closed",()->{PairSettings s=configured(new PairCtx());s.reserve(request(1),NOW);check(!s.reserve(request(2),NOW-1),"backwards time");});
        run("wrong host request rejected",()->{PairProtocol.Request r=new PairProtocol.Request("cd".repeat(16),"01".repeat(32),"ff".repeat(32),NOW/1000+40);check(!configured(new PairCtx()).reserve(r,NOW),"same PC only");});
        run("write failure prevents UI and closes feature",()->{PairCtx c=new PairCtx();PairSettings s=configured(c);c.prefs.fail=true;c.prefs.applyMemoryOnFailure=true;check(!s.reserve(request(1),NOW)&&!s.enabled(),"durable before UI");});
        run("corrupt ledger cannot replenish",()->{PairCtx c=new PairCtx();PairSettings s=configured(c);c.prefs.values.put("attempt_ledger","bad");check(!s.reserve(request(1),NOW),"corruption blocked");});
        run("diagnostic excludes host key and one-time nonce",()->{PairSettings s=configured(new PairCtx());s.reserve(request(1),NOW);String d=s.diagnostic();check(!d.contains(HOST)&&!d.contains(request(1).nonce),"no key identifiers");});
        run("bootstrap is never inherited on upgrade",()->check(!new PairSettings(new PairCtx()).bootstrapAllowed(),"explicit opt in"));
        run("foreground bootstrap approval can pin original authenticated host once",()->{PairSettings s=new PairSettings(new PairCtx());check(s.enableBootstrap()&&s.enabled()&&s.bootstrapAllowed(),"approved");check(s.pinAuthenticated(request(1))&&!s.bootstrapAllowed()&&s.fingerprint().equals(HOST),"fixed durably");});
        run("bootstrap cannot replace an already fixed host",()->{PairSettings s=configured(new PairCtx());check(!s.enableBootstrap(),"no replacement route");});
        run("missing bootstrap approval cannot learn a host",()->check(!new PairSettings(new PairCtx()).pinAuthenticated(request(1)),"no trust merely on arrival"));
        run("failed durable bootstrap pin prevents activation",()->{PairCtx c=new PairCtx();PairSettings s=new PairSettings(c);s.enableBootstrap();c.prefs.fail=true;check(!s.pinAuthenticated(request(1))&&!s.enabled(),"write failure stops");});
        run("older enabled state cannot inherit broader recovery policy",()->{PairCtx c=new PairCtx();c.prefs.values.put("host_key_sha256",HOST);c.prefs.values.put("enabled",true);check(!new PairSettings(c).enabled(),"new explicit policy approval required");});
        run("new approval preserves fixed original host",()->{PairCtx c=new PairCtx();c.prefs.values.put("host_key_sha256",HOST);c.prefs.values.put("enabled",true);PairSettings s=new PairSettings(c);check(s.enable(true)&&s.enabled()&&s.fingerprint().equals(HOST),"explicit and same computer");});
        run("new approval does not clear old retry ledger",()->{PairCtx c=new PairCtx();PairSettings s=configured(c);s.reserve(request(1),NOW);s.reserve(request(2),NOW+1);c.prefs.values.remove("policy_v2_approved");check(!s.enabled()&&s.enable(true)&&!s.reserve(request(3),NOW+2),"no budget refill on policy migration");});
        System.out.println("ALL "+count+" PAIR STORAGE TESTS PASSED");
    }
}
