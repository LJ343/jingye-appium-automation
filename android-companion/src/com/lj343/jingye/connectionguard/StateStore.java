package com.lj343.jingye.connectionguard;

import android.content.Context;
import android.content.SharedPreferences;
import org.json.JSONException;
import org.json.JSONObject;

/** Private, bounded diagnostics. Values never contain network identities or exception text. */
final class StateStore {
    private static volatile String lastCommit = "NOT_ATTEMPTED";
    private static volatile boolean faultObserved;
    static void markPlatformFault() { faultObserved = true; lastCommit = "FAILED"; }
    private final SharedPreferences prefs;
    StateStore(Context context) { prefs = context.getSharedPreferences("guard_state", Context.MODE_PRIVATE); }
    boolean desiredArmed() { return prefs.getBoolean("armed", false); }
    boolean consumed() { return prefs.getBoolean("attempt_consumed", false); }
    long attempts() { return prefs.getLong("attempts", 0); }
    int autoRetriesUsed() {
        long value = prefs.getLong("auto_retries_used", 0);
        if (value < 0 || value > RecoveryController.MAX_AUTO_RETRIES)
            throw new IllegalStateException("RETRY_RECORD_INVALID");
        return (int) value;
    }
    long retryNotBeforeMs() {
        long value = prefs.getLong("retry_not_before_ms", 0);
        if (value < 0) throw new IllegalStateException("RETRY_RECORD_INVALID");
        return value;
    }
    long[] recentAttempts() {
        String raw = prefs.getString("attempt_times", "");
        if (raw.isEmpty()) return new long[0];
        String[] fields = raw.split(",");
        long[] times = new long[fields.length];
        try { for (int i = 0; i < fields.length; i++) times[i] = Long.parseLong(fields[i]); }
        catch (NumberFormatException corrupt) {
            // Fail closed: a damaged rate-limit record cannot create an enable loop.
            return new long[]{Long.MAX_VALUE, Long.MAX_VALUE, Long.MAX_VALUE};
        }
        return times;
    }

    synchronized boolean save(RecoveryController.Snapshot s, boolean running) {
        long now = System.currentTimeMillis();
        String event = now + " " + s.phase + " " + s.code + " attempts=" + s.attempts
                + " auto_retries=" + s.autoRetriesUsed + " next_retry=" + s.retryNotBeforeMs;
        String prior = prefs.getString("last_event", "");
        String history = prefs.getString("history", "");
        String signature = s.phase + "/" + s.code + "/" + s.attempts + "/"
                + s.autoRetriesUsed + "/" + s.retryNotBeforeMs;
        StringBuilder times = new StringBuilder();
        for (long time : s.recentAttempts) { if (times.length() > 0) times.append(','); times.append(time); }
        if (!signature.equals(prior)) {
            String[] lines = history.isEmpty() ? new String[0] : history.split("\n");
            StringBuilder out = new StringBuilder();
            for (int i = Math.max(0, lines.length - 39); i < lines.length; i++) out.append(lines[i]).append('\n');
            history = out.append(event).toString();
        }
        return commit(prefs.edit().putBoolean("armed", s.armed).putBoolean("attempt_consumed", s.attemptConsumed)
                .putBoolean("service_running", running).putBoolean("wifi_available", s.wifi)
                .putBoolean("switch_enabled", s.enabled).putBoolean("timer_pending", s.timerPending)
                .putString("phase", s.phase).putString("code", s.code).putLong("attempts", s.attempts)
                .putLong("auto_retries_used", s.autoRetriesUsed).putLong("retry_not_before_ms", s.retryNotBeforeMs)
                .putLong("updated_at_ms", now).putString("last_event", signature)
                .putString("attempt_times", times.toString()).putString("history", history));
    }

    synchronized void serviceGone() {
        if (!commit(prefs.edit().putBoolean("service_running", false).putBoolean("timer_pending", false)
                .putLong("updated_at_ms", System.currentTimeMillis()))) throw new IllegalStateException("STATE_PERSIST_FAILED");
    }

    synchronized boolean platformFailure() {
        markPlatformFault();
        return commit(prefs.edit().putBoolean("armed", false).putBoolean("service_running", false)
                .putBoolean("timer_pending", false).putString("phase", "PAUSED")
                .putLong("retry_not_before_ms", 0)
                .putString("code", "PLATFORM_EXCEPTION").putLong("updated_at_ms", System.currentTimeMillis()));
    }

    private boolean commit(SharedPreferences.Editor edit) {
        try {
            boolean result = edit.commit(); lastCommit = result ? "SUCCEEDED" : "FAILED"; return result;
        } catch (RuntimeException failure) { lastCommit = "FAILED"; throw failure; }
    }

    synchronized String snapshot() {
        JSONObject json = new JSONObject();
        try {
            json.put("schema", 1);
            json.put("last_record_write_result_current_process", lastCommit);
            json.put("platform_fault_observed_current_process", faultObserved);
            json.put("armed", desiredArmed());
            json.put("attempt_consumed", consumed());
            json.put("service_running_last_report", prefs.getBoolean("service_running", false));
            json.put("wifi_available_last_report", prefs.getBoolean("wifi_available", false));
            json.put("switch_enabled_last_report", prefs.getBoolean("switch_enabled", false));
            json.put("timer_pending_last_report", prefs.getBoolean("timer_pending", false));
            json.put("phase", prefs.getString("phase", "STOPPED"));
            json.put("code", prefs.getString("code", "USER_STOPPED"));
            json.put("attempts", attempts());
            json.put("auto_retries_used", autoRetriesUsed());
            json.put("auto_retry_limit", RecoveryController.MAX_AUTO_RETRIES);
            json.put("next_retry_at_ms", retryNotBeforeMs());
            json.put("retry_policy", "Up to two retries after 60s/120s; persistent; no retry after permission/write/platform/rate faults");
            json.put("rate_limit", "3 attempts / 10 minutes; persisted across service recreation");
            json.put("updated_at_ms", prefs.getLong("updated_at_ms", 0));
            json.put("meaning", "Switch state only; PC ADB connectivity is unverified. Persisted state may be stale after OS kill.");
        } catch (JSONException impossible) { return "{\"code\":\"STATE_ENCODING_FAILED\"}"; }
        return json.toString();
    }
    String history() { return prefs.getString("history", ""); }
}
