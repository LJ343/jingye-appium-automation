package com.lj343.jingye.connectionguard;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Locale;

/** Exact AP identity; no scan, wildcard, subnet or SSID-only authorization. */
final class TrustIdentity {
    final String ssid, bssid, key;
    private TrustIdentity(String s, String b, String k) { ssid=s; bssid=b; key=k; }
    static TrustIdentity of(String ssid, String bssid) {
        if (ssid == null || bssid == null) return null;
        bssid=bssid.toLowerCase(Locale.ROOT);
        if (ssid.isEmpty() || ssid.equals("<unknown ssid>") || ssid.length()>128
                || !bssid.matches("[0-9a-f]{2}(:[0-9a-f]{2}){5}")
                || bssid.equals("02:00:00:00:00:00") || bssid.equals("00:00:00:00:00:00")
                || (Integer.parseInt(bssid.substring(0,2),16)&1)!=0) return null;
        for (int i=0;i<ssid.length();i++) if (Character.isISOControl(ssid.charAt(i))) return null;
        try {
            byte[] digest=MessageDigest.getInstance("SHA-256").digest(
                    ("jingye-ap-v1\0"+ssid+"\0"+bssid).getBytes(StandardCharsets.UTF_8));
            StringBuilder hex=new StringBuilder();
            for(byte b:digest) hex.append(String.format(Locale.ROOT,"%02x",b&255));
            return new TrustIdentity(ssid,bssid,hex.toString());
        } catch (java.security.NoSuchAlgorithmException impossible) { throw new IllegalStateException("HASH_UNAVAILABLE"); }
    }
}
