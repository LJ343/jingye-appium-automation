package com.lj343.jingye.connectionguard;

import android.Manifest;
import android.content.Context;
import android.content.pm.PackageManager;
import android.location.LocationManager;
import android.net.wifi.WifiInfo;
import android.net.wifi.WifiManager;

/** Reads current association only. Missing/redacted identity denies authorization. */
final class TrustWifi {
    static TrustIdentity read(Context c) {
        if(c.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)!=PackageManager.PERMISSION_GRANTED
                || !c.getSystemService(LocationManager.class).isLocationEnabled())return null;
        WifiInfo info=c.getSystemService(WifiManager.class).getConnectionInfo();
        if(info==null || info.getNetworkId()<0)return null;
        String ssid=info.getSSID();
        if(ssid!=null && ssid.startsWith("\"") && ssid.endsWith("\"") && ssid.length()>=2)
            ssid=ssid.substring(1,ssid.length()-1);
        return TrustIdentity.of(ssid,info.getBSSID());
    }
}
