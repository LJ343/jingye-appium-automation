package com.lj343.jingye.connectionguard;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Base64;
import org.json.JSONObject;

/** Provisioned locally by the shell; secrets are never part of diagnostics. */
final class ReporterSettings {
    static final String PREFS = "private_address_report";
    final String host, id;
    final int port;
    final byte[] key;
    private ReporterSettings(String host, int port, String id, String key) {
        if (!AddressProtocol.privateIpv4(host) || port < 1024 || port > 65535 || !AddressProtocol.hex(id, 32))
            throw new IllegalArgumentException("INVALID_REPORT_TARGET");
        this.host = host; this.port = port; this.id = id; this.key = AddressProtocol.unhex(key);
    }
    static ReporterSettings load(Context context) {
        SharedPreferences p = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        if (!p.contains("key")) return null;
        return new ReporterSettings(p.getString("host", ""), p.getInt("port", 0), p.getString("id", ""), p.getString("key", ""));
    }
    static boolean configure(Context context, String encoded) {
        try {
            if (encoded == null || encoded.length() > 1024) return false;
            JSONObject o = new JSONObject(new String(Base64.decode(encoded, Base64.NO_WRAP), java.nio.charset.StandardCharsets.UTF_8));
            if (o.length() != 4 || !(o.get("port") instanceof Integer)) return false;
            new ReporterSettings(o.getString("host"), o.getInt("port"), o.getString("id"), o.getString("key"));
            return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
                    .putString("host", o.getString("host")).putInt("port", o.getInt("port"))
                    .putString("id", o.getString("id")).putString("key", o.getString("key")).commit();
        } catch (Exception failure) { return false; }
    }
}
