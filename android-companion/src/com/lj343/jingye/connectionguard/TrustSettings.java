package com.lj343.jingye.connectionguard;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONObject;
import java.util.LinkedHashSet;
import java.util.Set;

/** Private hashes only. No exported configuration or diagnostic disclosure of AP identities. */
final class TrustSettings {
    private final SharedPreferences prefs;
    private static volatile boolean storageFault;
    TrustSettings(Context c) { prefs=c.getSharedPreferences("network_trust_v1",Context.MODE_PRIVATE); }
    private Set<String> entries() { return entries("allowlist",16); }
    private Set<String> entries(String field,int limit) {
        Set<String> out=new LinkedHashSet<>(); String raw=prefs.getString(field,"");
        if (!raw.isEmpty()) for(String k:raw.split("\n",-1)) {
            if(!k.matches("[0-9a-f]{64}") || !out.add(k)) throw new IllegalStateException("ALLOWLIST_INVALID");
        }
        if(out.size()>limit)throw new IllegalStateException("ALLOWLIST_INVALID");
        return out;
    }
    boolean enabled() { return !storageFault && prefs.getBoolean("enabled",false); }
    boolean contains(String key) { return !storageFault && entries().contains(key); }
    int count() { return entries().size(); }
    int ssidCount() { return entries("ssid_allowlist",8).size(); }
    String match(TrustIdentity id) {
        if(storageFault || id==null)return "NONE";
        // Validate both stores before granting either route. Never reinterpret old AP hashes as names.
        Set<String> aps=entries(),names=entries("ssid_allowlist",8);
        if(aps.contains(id.key))return "EXACT_AP";
        return names.contains(TrustIdentity.ssidKey(id.ssid))?"EXACT_SSID":"NONE";
    }
    boolean addSsid(String ssid) {
        String key=TrustIdentity.ssidKey(ssid);if(key==null)return false;
        Set<String> keys=entries("ssid_allowlist",8);keys.add(key);if(keys.size()>8)return false;
        return commit(prefs.edit().putString("ssid_allowlist",String.join("\n",keys)));
    }
    boolean removeSsid(String ssid) {
        String key=TrustIdentity.ssidKey(ssid);if(key==null)return false;
        Set<String> keys=entries("ssid_allowlist",8);keys.remove(key);
        SharedPreferences.Editor edit=prefs.edit().putString("ssid_allowlist",String.join("\n",keys));
        if(keys.isEmpty() && count()==0)edit.putBoolean("enabled",false);
        return commit(edit);
    }
    boolean add(String key) {
        if(key==null || !key.matches("[0-9a-f]{64}"))return false;
        Set<String> keys=entries(); keys.add(key); if(keys.size()>16)return false;
        return commit(prefs.edit().putString("allowlist",String.join("\n",keys)));
    }
    boolean enable(boolean value) {
        if(value && (storageFault || count()+ssidCount()==0))return false;
        return commit(prefs.edit().putBoolean("enabled",value));
    }
    boolean clear() { return commit(prefs.edit().putBoolean("enabled",false).putString("allowlist","")); }
    synchronized boolean reserve(TrustGuard.Action a,String key,int window) {
        if(!enabled() || !contains(key))return false;
        String next=TrustLedger.reserve(prefs.getString("action_ledger",""),a,key,window,System.currentTimeMillis());
        return next!=null && commit(prefs.edit().putString("action_ledger",next));
    }
    synchronized boolean reserve(TrustGuard.Action a,TrustIdentity id,int window) {
        if(!enabled() || "NONE".equals(match(id)))return false;
        String next=TrustLedger.reserve(prefs.getString("action_ledger",""),a,id.key,window,System.currentTimeMillis());
        return next!=null && commit(prefs.edit().putString("action_ledger",next));
    }
    void record(String code) {
        boolean action="ALWAYS_CLICK_SENT".equals(code) || "ALLOW_CLICK_SENT_PC_UNVERIFIED".equals(code);
        if(!action && code.equals(prefs.getString("last_code","")))return;
        long at=System.currentTimeMillis();
        SharedPreferences.Editor e=prefs.edit().putString("last_code",code).putLong("last_at_ms",at);
        if(action)e.putString("last_click_code",code).putLong("last_click_at_ms",at);
        commit(e);
    }
    private boolean commit(SharedPreferences.Editor e) {
        try { if(e.commit())return true; }
        catch(RuntimeException fault) { storageFault=true;throw fault; }
        storageFault=true;return false;
    }
    String diagnostic() {
        JSONObject j=new JSONObject();
        try {
            j.put("enabled",enabled()); j.put("allowlisted_ap_count",count());
            j.put("allowlisted_ssid_count",ssidCount());
            j.put("accessibility_connected_current_process",TrustAccessibilityService.connected);
            j.put("last_match_route_current_process",TrustAccessibilityService.lastRoute);
            j.put("last_root_available_current_process",TrustAccessibilityService.lastRootAvailable);
            j.put("last_system_window_current_process",TrustAccessibilityService.lastSystemWindow);
            j.put("last_layout_current_process",TrustAccessibilityService.lastLayout);
            j.put("last_window_type_current_process",TrustAccessibilityService.lastWindowType);
            j.put("guard_live_armed_current_process",GuardService.liveArmed);
            j.put("storage_fault_current_process",storageFault);
            j.put("last_code",prefs.getString("last_code","NOT_ATTEMPTED"));
            j.put("last_at_ms",prefs.getLong("last_at_ms",0));
            j.put("last_click_code",prefs.getString("last_click_code","NOT_ATTEMPTED"));
            j.put("last_click_at_ms",prefs.getLong("last_click_at_ms",0));
            j.put("policy","Explicit exact AP or exact Wi-Fi name + exact system dialog; shared 3 checkbox and 3 allow actions / 10 minutes; no unlock; PC connectivity unverified");
        } catch(Exception fault) { return "{\"last_code\":\"TRUST_DIAGNOSTIC_UNAVAILABLE\"}"; }
        return j.toString();
    }
}
