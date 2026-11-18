package com.lj343.jingye.connectionguard;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONObject;

/** One explicitly provisioned host key, opt-in, durable shared two-attempt budget. */
final class PairSettings {
    private final SharedPreferences prefs;private static volatile boolean fault;
    PairSettings(Context c){prefs=c.getSharedPreferences("pairing_recovery_v1",Context.MODE_PRIVATE);}
    String fingerprint(){String s=prefs.getString("host_key_sha256","");return AddressProtocol.hex(s,64)?s:"";}
    boolean bootstrapAllowed(){return !fault && fingerprint().isEmpty() && prefs.getBoolean("bootstrap_approved",false);}
    boolean enabled(){return !fault && (!fingerprint().isEmpty() || bootstrapAllowed()) && prefs.getBoolean("enabled",false);}
    boolean enableBootstrap(){return !fault && fingerprint().isEmpty() && commit(prefs.edit().putBoolean("bootstrap_approved",true).putBoolean("enabled",true));}
    boolean pinAuthenticated(PairProtocol.Request request){
        if(request==null || !AddressProtocol.hex(request.fingerprint,64))return false;
        if(!fingerprint().isEmpty())return fingerprint().equals(request.fingerprint);
        return enabled() && bootstrapAllowed() && commit(prefs.edit().putString("host_key_sha256",request.fingerprint).putBoolean("bootstrap_approved",false));
    }
    boolean pin(String value){
        if(!AddressProtocol.hex(value,64) || (!fingerprint().isEmpty() && !fingerprint().equals(value)))return false;
        return commit(prefs.edit().putString("host_key_sha256",value));
    }
    boolean enable(boolean value){return (!value || !fingerprint().isEmpty()) && commit(prefs.edit().putBoolean("enabled",value));}
    synchronized boolean reserve(PairProtocol.Request r,long now) {
        if(!enabled() || r==null || !r.fingerprint.equals(fingerprint()) || now<=0
                || r.expires<now/1000 || r.expires-now/1000>45)return false;
        String raw=prefs.getString("attempt_ledger","");if(raw.length()>512)return false;
        if(!raw.isEmpty() && raw.split("\n",-1).length>2)return false;
        StringBuilder next=new StringBuilder();int count=0;
        try {
            if(!raw.isEmpty())for(String line:raw.split("\n",-1)){
                String[] p=line.split(",",-1);if(p.length!=2 || !AddressProtocol.hex(p[1],64))return false;
                long at=Long.parseLong(p[0]);if(at<=0 || at>now || r.nonce.equals(p[1]))return false;
                if(now-at<600_000){if(next.length()>0)next.append('\n');next.append(line);count++;}
            }
        }catch(NumberFormatException invalid){return false;}
        if(count>=2)return false;
        if(next.length()>0)next.append('\n');next.append(now).append(',').append(r.nonce);
        return commit(prefs.edit().putString("attempt_ledger",next.toString()));
    }
    private boolean commit(SharedPreferences.Editor e){
        try {if(e.commit())return true;}catch(RuntimeException problem){fault=true;throw problem;}
        fault=true;return false;
    }
    void record(String code){
        if(code.equals(prefs.getString("last_code","")))return;
        commit(prefs.edit().putString("last_code",code).putLong("last_at_ms",System.currentTimeMillis()));
    }
    String diagnostic(){
        try {JSONObject j=new JSONObject();j.put("configured",!fingerprint().isEmpty());j.put("enabled",enabled());j.put("awaiting_authenticated_host_pin",bootstrapAllowed());
            j.put("last_code",prefs.getString("last_code","PAIR_NOT_ATTEMPTED"));j.put("last_at_ms",prefs.getLong("last_at_ms",0));
            j.put("policy","Pinned computer + trusted network + unlocked official Settings; encrypted code; 2 attempts / 10 minutes; no unlock");
            return j.toString();
        }catch(Exception e){return "{\"last_code\":\"PAIR_DIAGNOSTIC_UNAVAILABLE\"}";}
    }
}
