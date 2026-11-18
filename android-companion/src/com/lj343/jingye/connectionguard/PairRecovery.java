package com.lj343.jingye.connectionguard;

import android.app.KeyguardManager;
import android.content.Context;
import android.os.PowerManager;
import android.os.SystemClock;
import android.provider.Settings;

/** A short-lived request; no timer or UI activity exists without the pinned PC request. */
final class PairRecovery {
    private static Handle active;
    static final class Handle {
        final PairProtocol.Request request;final ReporterSettings config;final String ip,networkKey;
        final long deadline;volatile String packet;volatile String timeoutCode="PAIR_WAIT_SETTINGS_WINDOW";int connectionPort;
        Handle(PairProtocol.Request r,ReporterSettings c,String ip,String key){
            request=r;config=c;this.ip=ip;networkKey=key;
            deadline=SystemClock.elapsedRealtime()+Math.min(30_000,Math.max(0,r.expires*1000-System.currentTimeMillis()));
        }
        boolean ready(Context c){
            if(packet!=null)return false;
            if(SystemClock.elapsedRealtime()>=deadline){timeout(c);return false;}
            String block=block(c,request.fingerprint,networkKey);
            if(block!=null){fail(c,block);return false;}
            return true;
        }
        synchronized void fail(Context c,String code){
            if(packet!=null)return;
            packet=PairProtocol.status(request,config.key,code);new PairSettings(c).record(code);
        }
        void timeout(Context c){fail(c,timeoutCode);}
        synchronized void complete(Context c,int pairPort,String code){
            if(!ready(c))return;
            try {
                packet=PairProtocol.seal(request,config.key,ip,pairPort,code,connectionPort);
                new PairSettings(c).record("PAIR_CODE_ENCRYPTED_PC_UNVERIFIED");
            }catch(Exception bad){fail(c,"PAIR_ENCRYPTION_OR_FIELDS_FAILED");}
        }
    }
    static String block(Context c,String fingerprint,String expectedNetwork){
        PairSettings settings=new PairSettings(c);
        if(!settings.enabled() || !settings.fingerprint().equals(fingerprint))return "PAIR_DISABLED_OR_HOST_MISMATCH";
        if(!GuardService.liveArmed || !TrustAccessibilityService.connected)return "PAIR_GUARD_OR_ACCESSIBILITY_STOPPED";
        if(!c.getSystemService(PowerManager.class).isInteractive() || c.getSystemService(KeyguardManager.class).isKeyguardLocked())return "PAIR_SCREEN_LOCKED_OR_OFF";
        if(Settings.Global.getInt(c.getContentResolver(),"adb_wifi_enabled",0)!=1)return "PAIR_WIRELESS_SWITCH_OFF";
        TrustSettings trust=new TrustSettings(c);TrustIdentity id=TrustWifi.read(c);
        if(id==null)return "PAIR_NETWORK_IDENTITY_UNAVAILABLE";
        if(!trust.enabled() || "NONE".equals(trust.match(id)))return "PAIR_NETWORK_NOT_PREAUTHORIZED";
        if(expectedNetwork!=null && !expectedNetwork.equals(id.key))return "PAIR_NETWORK_CHANGED";
        return null;
    }
    static synchronized Handle begin(Context c,ReporterSettings config,PairProtocol.Request request,String ip){
        if(request==null)return null;
        // The request is already HMAC-verified on the connected socket to the
        // previously provisioned computer. First pin additionally needs the
        // user's independent foreground bootstrap approval; never trust by IP.
        if(!new PairSettings(c).pinAuthenticated(request))return null;
        String reason=block(c,request.fingerprint,null);
        TrustIdentity id=TrustWifi.read(c);
        Handle h=new Handle(request,config,ip,id==null?"":id.key);
        if(reason!=null){h.fail(c,reason);return h;}
        if(active!=null && active.packet==null && SystemClock.elapsedRealtime()<active.deadline){h.fail(c,"PAIR_ALREADY_IN_PROGRESS");return h;}
        if(!new PairSettings(c).reserve(request,System.currentTimeMillis())){h.fail(c,"PAIR_RATE_OR_STORAGE_BLOCKED");return h;}
        active=h;new PairSettings(c).record("PAIR_REQUEST_AUTHENTICATED");
        TrustAccessibilityService.beginPairing(h);return h;
    }
}
