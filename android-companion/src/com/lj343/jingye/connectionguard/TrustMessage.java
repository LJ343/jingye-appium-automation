package com.lj343.jingye.connectionguard;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Parses only the exact localized SystemUI template, including both AP fields. */
final class TrustMessage {
    static final String SSID="\uE000JINGYE_SSID\uE001", BSSID="\uE000JINGYE_BSSID\uE001";
    static final class Parsed {
        final TrustIdentity identity; final String ssid,bssid;
        Parsed(TrustIdentity i,String s,String b){identity=i;ssid=s;bssid=b;}
    }
    // Honor's platform SystemUI presents exact localized labels and separate AP value nodes.
    static Parsed honorSplit(String expectedSsidLabel,String expectedBssidLabel,
            String ssidLabel,String ssid,String bssidLabel,String bssid) {
        if(expectedSsidLabel==null || expectedSsidLabel.isEmpty() || expectedBssidLabel==null
                || expectedBssidLabel.isEmpty() || !expectedSsidLabel.equals(ssidLabel)
                || !expectedBssidLabel.equals(bssidLabel))return null;
        TrustIdentity identity=TrustIdentity.of(ssid,bssid);
        return identity==null?null:new Parsed(identity,ssid,bssid);
    }
    static String splitMessage(String ssidLabel,String ssid,String bssidLabel,String bssid) {
        StringBuilder out=new StringBuilder("HONOR_SPLIT_V1");
        for(String value:new String[]{ssidLabel,ssid,bssidLabel,bssid}) {
            if(value==null)return null;
            out.append(':').append(value.length()).append(':').append(value);
        }
        return out.toString();
    }
    static Parsed parse(String template,String message) {
        if(template==null || message==null || template.length()>4096 || message.length()>4096)return null;
        int s=template.indexOf(SSID),b=template.indexOf(BSSID);
        if(s<0 || b<0 || s==b || template.indexOf(SSID,s+1)>=0 || template.indexOf(BSSID,b+1)>=0)return null;
        String first=s<b?SSID:BSSID, second=s<b?BSSID:SSID;
        int a=Math.min(s,b),z=Math.max(s,b);
        String ss="(?<ssid>[^\\r\\n]{1,128})",bs="(?<bssid>(?i:[0-9a-f]{2}(?::[0-9a-f]{2}){5}))";
        String regex=Pattern.quote(template.substring(0,a))+(s<b?ss:bs)
                +Pattern.quote(template.substring(a+first.length(),z))+(s<b?bs:ss)
                +Pattern.quote(template.substring(z+second.length()));
        Matcher match=Pattern.compile(regex).matcher(message);
        if(!match.matches())return null;
        String ssid=match.group("ssid"),bssid=match.group("bssid");
        TrustIdentity identity=TrustIdentity.of(ssid,bssid);
        return identity==null?null:new Parsed(identity,ssid,bssid);
    }
}
