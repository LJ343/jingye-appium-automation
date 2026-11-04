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
        public final long attempts;
        public final long[] recentAttempts;
        Snapshot(boolean armed, boolean wifi, boolean enabled, boolean consumed,
                 boolean pending, String phase, String code, long attempts, long[] recentAttempts) {
            this.armed = armed; this.wifi = wifi; this.enabled = enabled;
            this.attemptConsumed = consumed; this.timerPending = pending;
            this.phase = phase; this.code = code; this.attempts = attempts;
            this.recentAttempts = recentAttempts.clone();
        }
    }

    private final Port port;
    private final Scheduler scheduler;
    private boolean armed, wifi, enabled, attemptConsumed, seenEnabled, externalWait;
    private String phase = "STOPPED", code = "USER_STOPPED";
    private long attempts, generation;
    private Runnable pending;
    private final List<Long> recentAttempts = new ArrayList<>();

    public RecoveryController(Port port, Scheduler scheduler, long previousAttempts) {
        this(port, scheduler, previousAttempts, new long[0]);
    }

    public RecoveryController(Port port, Scheduler scheduler, long previousAttempts, long[] previousTimes) {
        this.port = port; this.scheduler = scheduler; this.attempts = previousAttempts;
        for (long time : previousTimes) recentAttempts.add(time);
    }

    public Snapshot getState() {
        long[] times = new long[recentAttempts.size()];
        for (int i = 0; i < times.length; i++) times[i] = recentAttempts.get(i);
        return new Snapshot(armed, wifi, enabled, attemptConsumed, pending != null,
                phase, code, attempts, times);
    }

    /** A user start resets the episode budget, never the persisted short-term rate limit. */
    public void start(boolean restoredConsumed) {
        cancelTimer(); port.releaseWake();
        armed = true; attemptConsumed = restoredConsumed; seenEnabled = false; externalWait = false;
        wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
        if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
        // A consumed episode is restored read-only, including after service death.
        if (attemptConsumed) { waitForExternalOn(); return; }
        if (!wifi) { set("WAIT_WIFI", "WAIT_WIFI"); return; }
        if (enabled) {
            set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
            return;
        }
        stabilize();
    }

    public void stop() {
        armed = false; externalWait = false; cancelTimer(); port.releaseWake();
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
        // Network loss/return must not replenish an unverified consumed episode.
        if (externalWait) {
            if (!available) resetExternalObservation();
            else observeExternalOn();
            return;
        }
        if (!available) {
            cancelTimer(); port.releaseWake();
            set("WAIT_WIFI", "WAIT_WIFI"); return;
        }
        if (previously) { publish(); return; }
        // An actual Wi-Fi loss/return starts a new recovery episode.
        attemptConsumed = false;
        enabled = port.readEnabled() == 1;
        if (enabled) { set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED"); }
        else { stabilize(); }
    }

    public void settingChanged() {
        enabled = port.readEnabled() == 1;
        if (!armed) { publish(); return; }
        if (externalWait) {
            if (!enabled) resetExternalObservation();
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
        later(STABLE_MS, this::attempt);
        publish();
    }

    private void attempt() {
        if (!armed) return;
        wifi = port.wifiAvailable();
        if (!wifi) { port.releaseWake(); set("WAIT_WIFI", "WAIT_WIFI"); return; }
        enabled = port.readEnabled() == 1;
        if (enabled) { port.releaseWake(); set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED"); return; }
        if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
        if (attemptConsumed) { waitForExternalOn(); return; }
        long now = port.nowMillis();
        // A backward wall-clock jump keeps future timestamps, conservatively limiting writes.
        recentAttempts.removeIf(time -> now >= time && now - time >= RATE_WINDOW_MS);
        if (recentAttempts.size() >= MAX_ATTEMPTS_PER_WINDOW) { pause("RATE_LIMITED"); return; }
        recentAttempts.add(now);
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
            attemptConsumed = false;
            set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
        });
        publish();
    }

    /** Keep the user's start intent and listeners, but never repeat this episode's write. */
    private void waitForExternalOn() {
        externalWait = true; attemptConsumed = true;
        resetExternalObservation();
        observeExternalOn();
    }

    private void resetExternalObservation() {
        cancelTimer(); port.releaseWake();
        set("WAIT_EXTERNAL_ON", "WAIT_EXTERNAL_ON");
    }

    /** Event-driven read-only observation: only a genuine stable ON can rearm. */
    private void observeExternalOn() {
        if (!armed || !externalWait) return;
        wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
        if (!wifi || !enabled) {
            resetExternalObservation();
            return;
        }
        if ("EXTERNAL_ON_STABILIZING".equals(phase)) { publish(); return; }
        set("EXTERNAL_ON_STABILIZING", "WAIT_EXTERNAL_ON_STABLE");
        later(REARM_MS, () -> {
            if (!armed || !externalWait) return;
            wifi = port.wifiAvailable(); enabled = port.readEnabled() == 1;
            if (!wifi || !enabled) {
                // This timer has fired; do not preserve a stabilizing phase with no timer.
                resetExternalObservation(); observeExternalOn(); return;
            }
            if (!port.canWrite()) { pause("PERMISSION_MISSING"); return; }
            externalWait = false; attemptConsumed = false;
            set("OPEN_PENDING_PC", "SWITCH_ON_PC_UNVERIFIED");
        });
        publish();
    }

    private void pause(String reason) {
        armed = false; externalWait = false; cancelTimer(); port.releaseWake();
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
