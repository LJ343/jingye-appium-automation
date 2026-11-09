package com.lj343.jingye.connectionguard;

import java.util.ArrayList;
import java.util.List;

/** Main-thread-only, Android-independent recovery policy. No network identity is read. */
public final class RecoveryController {
    public static final long STABLE_MS = 4_000;
    public static final long VERIFY_MS = 12_000;
    public static final long WAKE_WINDOW_MS = 25_000;
    public static final long REARM_MS = 30_000;
    public static final long RATE_WINDOW_MS = 600_000;
    public static final int MAX_ATTEMPTS_PER_WINDOW = 3;
    public static final int MAX_AUTO_RETRIES = 2;
    public static final long FIRST_RETRY_MS = 60_000;
    public static final long SECOND_RETRY_MS = 120_000;

    public interface Port {
        boolean wifiAvailable();
        int readEnabled();
        boolean canWrite();
        boolean enableOnce();
        long nowMillis();
        void holdWake(long timeoutMs);
        void releaseWake();
        void changed(Snapshot state);
    }

    public interface Scheduler {
        void schedule(Runnable work, long delayMs);
        void cancel(Runnable work);
    }

    public static final class Snapshot {
        public final boolean armed, wifi, enabled, attemptConsumed, timerPending;
        public final String phase, code;
        public final long attempts, retryNotBeforeMs;
        public final int autoRetriesUsed;
        public final long[] recentAttempts;
        Snapshot(boolean armed, boolean wifi, boolean enabled, boolean consumed,
                 boolean pending, String phase, String code, long attempts, long[] recentAttempts,
                 int autoRetriesUsed, long retryNotBeforeMs) {
            this.armed = armed; this.wifi = wifi; this.enabled = enabled;
            this.attemptConsumed = consumed; this.timerPending = pending;
            this.phase = phase; this.code = code; this.attempts = attempts;
            this.recentAttempts = recentAttempts.clone();
            this.autoRetriesUsed = autoRetriesUsed; this.retryNotBeforeMs = retryNotBeforeMs;
        }
    }

    private final Port port;
    private final Scheduler scheduler;
    private boolean armed, wifi, enabled, attemptConsumed, seenEnabled, externalWait;
    private String phase = "STOPPED", code = "USER_STOPPED";
    private long attempts, generation;
    private int autoRetriesUsed;
    private long retryNotBeforeMs;
    private Runnable pending;
    private final List<Long> recentAttempts = new ArrayList<>();

    public RecoveryController(Port port, Scheduler scheduler, long previousAttempts) {
        this(port, scheduler, previousAttempts, new long[0]);
    }

    public RecoveryController(Port port, Scheduler scheduler, long previousAttempts, long[] previousTimes) {
        this(port, scheduler, previousAttempts, previousTimes, 0, 0);
    }

    public RecoveryController(Port port, Scheduler scheduler, long previousAttempts, long[] previousTimes,
                              int previousAutoRetries, long previousRetryAt) {
        if (previousAutoRetries < 0 || previousAutoRetries > MAX_AUTO_RETRIES || previousRetryAt < 0)
            throw new IllegalArgumentException("INVALID_RETRY_RECORD");
        this.port = port; this.scheduler = scheduler; this.attempts = previousAttempts;
        this.autoRetriesUsed = previousAutoRetries; this.retryNotBeforeMs = previousRetryAt;
        for (long time : previousTimes) recentAttempts.add(time);
    }

    public Snapshot getState() {
        long[] times = new long[recentAttempts.size()];
        for (int i = 0; i < times.length; i++) times[i] = recentAttempts.get(i);
        return new Snapshot(armed, wifi, enabled, attemptConsumed, pending != null,
                phase, code, attempts, times, autoRetriesUsed, retryNotBeforeMs);
    }

    /** A user start resets the episode budget, never the persisted short-term rate limit. */
    public void start(boolean restoredConsumed) {
        cancelTimer(); port.releaseWake();
        armed = true; attemptConsumed = restoredConsumed; seenEnabled = false; externalWait = false;
        if (!restoredConsumed) { autoRetriesUsed = 0; retryNotBeforeMs = 0; }
        wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
        if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
        // Recreate the same persisted episode; neither retry count nor global rate resets.
        if (attemptConsumed) { waitForExternalOn(); return; }
        if (!wifi) { set("WAIT_WIFI", "WAIT_WIFI"); return; }
        if (enabled) {
            set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
            return;
        }
        stabilize();
    }

    public void stop() {
        armed = false; externalWait = false; retryNotBeforeMs = 0; autoRetriesUsed = 0;
        cancelTimer(); port.releaseWake();
        set("STOPPED", "USER_STOPPED");
    }

    /** Disposal does not persist a user stop; START_STICKY can restore the saved intent. */
    public void dispose() {
        armed = false; externalWait = false; cancelTimer(); port.releaseWake();
    }

    public void networkChanged(boolean available) {
        boolean previously = wifi;
        wifi = available;
        if (!armed) { publish(); return; }
        // Network loss/return never replenishes an unverified consumed episode.
        if (externalWait) {
            if (!available) waitForRetryWifi();
            else observeExternalOn();
            return;
        }
        if (!available) {
            if (attemptConsumed) {
                externalWait = true; ensureRetryDeadline(); waitForRetryWifi(); return;
            }
            cancelTimer(); port.releaseWake();
            set("WAIT_WIFI", "WAIT_WIFI"); return;
        }
        if (previously) { publish(); return; }
        // An actual Wi-Fi loss/return starts a new recovery episode.
        attemptConsumed = false;
        autoRetriesUsed = 0; retryNotBeforeMs = 0;
        enabled = port.readEnabled() == 1;
        if (enabled) { set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED"); }
        else { stabilize(); }
    }

    public void settingChanged() {
        enabled = port.readEnabled() == 1;
        if (!armed) { publish(); return; }
        if (externalWait) {
            if (!enabled) scheduleRetryWait();
            else observeExternalOn();
            return;
        }
        if ("VERIFYING".equals(phase)) {
            if (enabled) { seenEnabled = true; publish(); }
            else if (seenEnabled) { waitForExternalOn(); }
            else { publish(); }
            return;
        }
        if (!wifi) { publish(); return; }
        if (enabled) {
            if ("COOLDOWN".equals(phase)) { publish(); return; }
            cancelTimer(); port.releaseWake();
            set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
        } else if (!"STABILIZING".equals(phase)) {
            // A reset before stability waits for a genuine external enable, without writing.
            if (attemptConsumed) { waitForExternalOn(); }
            else { stabilize(); }
        }
    }

    private void stabilize() {
        if (!armed) return;
        cancelTimer();
        port.holdWake(WAKE_WINDOW_MS);
        set("STABILIZING", "WAIT_STABLE_WIFI");
        later(STABLE_MS, () -> attempt(false));
        publish();
    }

    private void attempt(boolean retry) {
        if (!armed) return;
        wifi = port.wifiAvailable();
        if (!wifi) {
            if (retry) { waitForRetryWifi(); }
            else { port.releaseWake(); set("WAIT_WIFI", "WAIT_WIFI"); }
            return;
        }
        enabled = port.readEnabled() == 1;
        if (enabled) {
            port.releaseWake();
            if (retry) observeExternalOn();
            else set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
            return;
        }
        if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
        if (retry) {
            if (!externalWait || !attemptConsumed || autoRetriesUsed >= MAX_AUTO_RETRIES
                    || retryNotBeforeMs == 0 || port.nowMillis() < retryNotBeforeMs) {
                waitForExternalOn(); return;
            }
        } else if (attemptConsumed) { waitForExternalOn(); return; }
        long now = port.nowMillis();
        // A backward wall-clock jump keeps future timestamps, conservatively limiting writes.
        recentAttempts.removeIf(time -> now >= time && now - time >= RATE_WINDOW_MS);
        if (recentAttempts.size() >= MAX_ATTEMPTS_PER_WINDOW) { pause("RATE_LIMITED"); return; }
        recentAttempts.add(now);
        if (retry) autoRetriesUsed++;
        retryNotBeforeMs = 0; externalWait = false;
        attemptConsumed = true;
        attempts++;
        seenEnabled = false;
        set("VERIFYING", "VERIFY_SWITCH_ONLY");
        // Persist the consumed budget BEFORE the write, so process death cannot loop it.
        if (!port.enableOnce()) { pause("SETTING_WRITE_REJECTED"); return; }
        enabled = port.readEnabled() == 1;
        seenEnabled = enabled;
        later(VERIFY_MS, () -> {
            if (!armed) return;
            wifi = port.wifiAvailable();
            if (!wifi) { networkChanged(false); return; }
            enabled = port.readEnabled() == 1;
            port.releaseWake();
            if (enabled) coolDown(REARM_MS - VERIFY_MS);
            else waitForExternalOn();
        });
        publish();
    }

    private void coolDown(long delay) {
        set("COOLDOWN", "SWITCH_ON_PC_UNVERIFIED");
        later(delay, () -> {
            if (!armed) return;
            wifi = port.wifiAvailable();
            if (!wifi) { networkChanged(false); return; }
            enabled = port.readEnabled() == 1;
            if (!enabled) { waitForExternalOn(); return; }
            completeRecovery();
        });
        publish();
    }

    /** A soft failure gets at most two persisted, backed-off retries, never a loop. */
    private void waitForExternalOn() {
        externalWait = true; attemptConsumed = true;
        cancelTimer(); port.releaseWake();
        observeExternalOn();
    }

    private void ensureRetryDeadline() {
        if (retryNotBeforeMs != 0 || autoRetriesUsed >= MAX_AUTO_RETRIES) return;
        long delay = autoRetriesUsed == 0 ? FIRST_RETRY_MS : SECOND_RETRY_MS;
        long now = port.nowMillis();
        retryNotBeforeMs = now > Long.MAX_VALUE - delay ? Long.MAX_VALUE : now + delay;
    }

    private void waitForRetryWifi() {
        cancelTimer(); port.releaseWake();
        if (autoRetriesUsed >= MAX_AUTO_RETRIES) {
            retryNotBeforeMs = 0; set("WAIT_EXTERNAL_ON", "RETRY_EXHAUSTED"); return;
        }
        ensureRetryDeadline();
        set("WAIT_WIFI_RETRY", "WAIT_RETRY_WIFI");
    }

    private void scheduleRetryWait() {
        if (!armed || !externalWait) return;
        if (!wifi) { waitForRetryWifi(); return; }
        if (autoRetriesUsed >= MAX_AUTO_RETRIES) {
            cancelTimer(); port.releaseWake(); retryNotBeforeMs = 0;
            set("WAIT_EXTERNAL_ON", "RETRY_EXHAUSTED"); return;
        }
        ensureRetryDeadline();
        if (pending != null && ("RETRY_WAIT".equals(phase) || "RETRY_STABILIZING".equals(phase))) {
            publish(); return; // Duplicate OFF/network events cannot reset or postpone this timer.
        }
        cancelTimer(); port.releaseWake();
        long now = port.nowMillis();
        long remaining = retryNotBeforeMs <= now ? 0 : retryNotBeforeMs - now;
        if (remaining < 0) remaining = Long.MAX_VALUE;
        set("RETRY_WAIT", "WAIT_RETRY");
        later(Math.min(remaining, SECOND_RETRY_MS), this::beginRetryStabilization);
        publish();
    }

    private void beginRetryStabilization() {
        if (!armed || !externalWait) return;
        wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
        if (!wifi) { waitForRetryWifi(); return; }
        if (enabled) { observeExternalOn(); return; }
        if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
        if (port.nowMillis() < retryNotBeforeMs) { scheduleRetryWait(); return; }
        port.holdWake(WAKE_WINDOW_MS);
        set("RETRY_STABILIZING", "WAIT_STABLE_RETRY");
        later(STABLE_MS, () -> attempt(true));
        publish();
    }

    private void completeRecovery() {
        cancelTimer(); port.releaseWake();
        externalWait = false; attemptConsumed = false;
        autoRetriesUsed = 0; retryNotBeforeMs = 0;
        set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
    }

    /** Event-driven read-only observation: only a genuine stable ON can rearm. */
    private void observeExternalOn() {
        if (!armed || !externalWait) return;
        wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
        if (!wifi || !enabled) {
            scheduleRetryWait();
            return;
        }
        if ("EXTERNAL_ON_STABILIZING".equals(phase) && pending != null) { publish(); return; }
        cancelTimer(); port.releaseWake();
        set("EXTERNAL_ON_STABILIZING", "WAIT_EXTERNAL_ON_STABLE");
        later(REARM_MS, () -> {
            if (!armed || !externalWait) return;
            wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
            if (!wifi || !enabled) {
                // This timer has fired; do not preserve a stabilizing phase with no timer.
                cancelTimer(); port.releaseWake();
                set("RETRY_WAIT", "WAIT_RETRY"); observeExternalOn(); return;
            }
            if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
            completeRecovery();
        });
        publish();
    }

    private void pause(String reason) {
        armed = false; externalWait = false; retryNotBeforeMs = 0; cancelTimer(); port.releaseWake();
        set("PAUSED", reason);
    }

    private void later(long delay, Runnable work) {
        cancelTimer();
        final long token = generation;
        pending = () -> {
            if (token != generation || !armed) return;
            pending = null;
            work.run();
        };
        scheduler.schedule(pending, delay);
    }

    private void cancelTimer() {
        generation++;
        if (pending != null) { scheduler.cancel(pending); pending = null; }
    }

    private void set(String nextPhase, String nextCode) {
        phase = nextPhase; code = nextCode; publish();
    }

    private void publish() { port.changed(getState()); }
}
