package com.lj343.jingye.connectionguard;

import java.util.ArrayList;
import java.util.List;

/** Deterministic adversarial event tests; uses the production policy, no Android runtime. */
public final class RecoveryControllerTest {
    private static int tests;
    private static final class Rig implements RecoveryController.Port, RecoveryController.Scheduler {
        boolean wifi = true, permission = true, acceptWrite = true, latchWrite = true;
        boolean throwWrite, throwRelease;
        int flag, writes, maxPending;
        boolean wake;
        long now, due;
        Runnable queued;
        final List<Runnable> captured = new ArrayList<>();
        final List<RecoveryController.Snapshot> states = new ArrayList<>();
        final RecoveryController c;
        Rig() { this(0, new long[0]); }
        Rig(long attempts, long[] times) { c = new RecoveryController(this, this, attempts, times); }
        public boolean wifiAvailable() { return wifi; }
        public int readEnabled() { return flag; }
        public boolean canWrite() { return permission; }
        public long nowMillis() { return now; }
        public boolean enableOnce() {
            writes++; if (throwWrite) throw new IllegalStateException("TEST_PLATFORM_FAILURE");
            if (acceptWrite && latchWrite) flag = 1; return acceptWrite;
        }
        public void holdWake(long ms) { check(ms <= 30_000, "bounded wake"); wake = true; }
        public void releaseWake() { if (throwRelease) throw new IllegalStateException("TEST_RELEASE_FAILURE"); wake = false; }
        public void changed(RecoveryController.Snapshot s) { states.add(s); }
        public void schedule(Runnable work, long delay) {
            check(queued == null, "only one pending task"); queued = work; due = now + delay;
            captured.add(work); maxPending = 1;
        }
        public void cancel(Runnable work) { if (queued == work) queued = null; }
        void advance(long ms) {
            long target = now + ms;
            while (queued != null && due <= target) {
                Runnable r = queued; queued = null; now = due; r.run();
            }
            now = target;
        }
        void set(int value) { flag = value; c.settingChanged(); }
        void net(boolean value) { wifi = value; c.networkChanged(value); }
    }
    private static void check(boolean ok, String text) { if (!ok) throw new AssertionError(text); }
    private static void phase(Rig r, String expected) { check(expected.equals(r.c.getState().phase), expected + " phase"); }
    private static void test(String name, Runnable work) { work.run(); tests++; System.out.println("PASS " + name); }

    public static void main(String[] args) {
        test("default inert and unsolicited events cannot write", () -> {
            Rig r = new Rig(); r.set(0); r.net(true); r.advance(100_000);
            check(r.writes == 0 && !r.wake, "default inert"); phase(r, "STOPPED");
        });
        test("explicit start waits four seconds and verifies twelve seconds", () -> {
            Rig r = new Rig(); r.c.start(false); phase(r, "STABILIZING");
            r.advance(3_999); check(r.writes == 0, "no early write");
            r.advance(1); check(r.writes == 1 && r.wake, "one write"); phase(r, "VERIFYING");
            r.advance(11_999); phase(r, "VERIFYING"); r.advance(1);
            phase(r, "COOLDOWN"); check(!r.wake, "release after verify");
            r.advance(18_000); phase(r, "OPEN_PENDING_PC"); check(r.queued == null, "rearm after stable thirty seconds");
        });
        test("stop invalidates even an already dispatched old callback", () -> {
            Rig r = new Rig(); r.c.start(false); Runnable stale = r.queued; r.c.stop(); stale.run(); r.advance(100_000);
            check(r.writes == 0 && !r.wake && r.queued == null, "stop cancels writes"); phase(r, "STOPPED");
        });
        test("rearm cannot execute stale generation", () -> {
            Rig r = new Rig(); r.c.start(false); Runnable stale = r.queued; r.c.stop(); r.c.start(false);
            stale.run(); check(r.writes == 0, "old arm invalid"); r.advance(4_000); check(r.writes == 1, "new arm only");
        });
        test("network loss during stabilization waits and restabilizes", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(2_000); r.net(false);
            r.advance(50_000); phase(r, "WAIT_WIFI"); check(r.writes == 0 && !r.wake, "offline quiet");
            r.net(true); r.advance(3_999); check(r.writes == 0, "restabilize"); r.advance(1); check(r.writes == 1, "return attempt");
        });
        test("Wi-Fi loss at write deadline is rechecked", () -> {
            Rig r = new Rig(); r.c.start(false); r.wifi = false; r.advance(4_000);
            check(r.writes == 0 && !r.wake, "deadline reads network"); phase(r, "WAIT_WIFI");
        });
        test("framework immediate reset pauses and never loops", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(4_000); r.set(0); phase(r, "PAUSED");
            for (int i = 0; i < 100; i++) { r.set(0); r.net(false); r.net(true); r.advance(20_000); }
            check(r.writes == 1 && !r.wake && r.queued == null, "no rejection loop");
        });
        test("a write that never latches pauses at deadline", () -> {
            Rig r = new Rig(); r.latchWrite = false; r.c.start(false); r.advance(16_000);
            phase(r, "PAUSED"); check(r.writes == 1 && !r.wake, "bounded failed write");
        });
        test("setting failure and permission revocation cannot repeat", () -> {
            Rig rejected = new Rig(); rejected.acceptWrite = false; rejected.c.start(false); rejected.advance(4_000);
            phase(rejected, "PAUSED"); check(rejected.writes == 1, "one rejected write");
            Rig revoked = new Rig(); revoked.c.start(false); revoked.permission = false; revoked.advance(4_000);
            phase(revoked, "PAUSED"); check(revoked.writes == 0, "permission rechecked");
        });
        test("already-on start and later independent same-handle episodes", () -> {
            Rig r = new Rig(); r.flag = 1; r.c.start(false); phase(r, "OPEN_PENDING_PC");
            check(r.writes == 0 && !r.wake, "on stays read only"); r.set(0); r.advance(34_000);
            check(r.writes == 1, "observer recovery"); phase(r, "OPEN_PENDING_PC");
            r.set(0); r.advance(34_000); phase(r, "OPEN_PENDING_PC"); check(r.writes == 2, "second independent episode allowed");
        });
        test("reset during thirty-second cooldown pauses", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(16_000); phase(r, "COOLDOWN");
            r.set(0); r.advance(100_000); phase(r, "PAUSED"); check(r.writes == 1, "early second off pauses");
        });
        test("manual resume explicitly replenishes attempt", () -> {
            Rig r = new Rig(); r.latchWrite = false; r.c.start(false); r.advance(16_000); phase(r, "PAUSED");
            r.latchWrite = true; r.c.start(false); r.advance(34_000); check(r.writes == 2, "explicit second attempt"); phase(r, "OPEN_PENDING_PC");
        });
        test("sticky recreation keeps consumed attempt budget", () -> {
            Rig r = new Rig(); r.c.start(true); r.advance(100_000);
            phase(r, "PAUSED"); check(r.writes == 0, "restored consumed budget cannot write");
        });
        test("network loss after successful recovery creates a new episode", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(34_000); r.net(false); r.set(0); r.net(true); r.advance(34_000);
            check(r.writes == 2, "one per real network return"); phase(r, "OPEN_PENDING_PC");
        });
        test("duplicate available and observer callbacks never multiply timers", () -> {
            Rig r = new Rig(); r.c.start(false);
            for (int i = 0; i < 100; i++) { r.net(true); r.set(0); }
            r.advance(4_000); for (int i = 0; i < 100; i++) { r.net(true); r.set(1); }
            r.advance(12_000); check(r.writes == 1 && r.maxPending == 1, "single pending timer");
        });
        test("stop during verify releases wake and never resumes", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(4_000); Runnable stale = r.queued;
            r.c.stop(); stale.run(); r.set(0); r.advance(100_000);
            check(r.writes == 1 && !r.wake && r.queued == null, "stop during verify");
        });
        test("external enable during stabilization cancels the write", () -> {
            Rig r = new Rig(); r.c.start(false); r.set(1); r.advance(100_000);
            check(r.writes == 0 && !r.wake, "external enable honored"); phase(r, "OPEN_PENDING_PC");
        });
        test("three attempts in ten minutes trips a persistent rate circuit", () -> {
            Rig r = new Rig();
            for (int i = 0; i < 3; i++) { r.flag = 0; r.c.start(false); r.advance(34_000); }
            r.set(0); r.advance(4_000); phase(r, "PAUSED");
            check(r.writes == 3 && "RATE_LIMITED".equals(r.c.getState().code), "fourth write blocked");
            r.c.start(false); r.advance(4_000); check(r.writes == 3, "manual resume cannot bypass rate limit");
            RecoveryController.Snapshot snapshot = r.c.getState();
            Rig restored = new Rig(snapshot.attempts, snapshot.recentAttempts); restored.now = r.now;
            restored.c.start(false); restored.advance(4_000); check(restored.writes == 0, "recreation cannot bypass rate limit");
            restored.advance(RecoveryController.RATE_WINDOW_MS); restored.c.start(false); restored.advance(34_000);
            check(restored.writes == 1, "expired window permits explicit recovery");
        });
        test("sticky recreation of consumed-on state rechecks thirty-second stability", () -> {
            Rig r = new Rig(); r.flag = 1; r.c.start(true); phase(r, "COOLDOWN");
            r.advance(29_999); check(r.c.getState().attemptConsumed, "not rearmed early");
            r.advance(1); phase(r, "OPEN_PENDING_PC"); check(!r.c.getState().attemptConsumed && r.writes == 0, "restored stable state rearmed without write");
        });
        test("platform write exception reaches a fail-closed boundary without retry", () -> {
            Rig r = new Rig(); r.throwWrite = true; r.c.start(false);
            boolean[] teardown = {false};
            boolean success = FailureBoundary.run(() -> r.advance(4_000), () -> {
                r.c.dispose(); teardown[0] = true;
            });
            check(!success && teardown[0] && !r.c.getState().armed && !r.wake, "boundary disposes after exception");
            r.set(0); r.net(false); r.net(true); r.advance(100_000);
            check(r.writes == 1 && r.c.getState().attemptConsumed, "exception does not replenish or retry write");
        });
        test("best-effort cleanup runs every step after platform cleanup failures", () -> {
            int[] steps = {0};
            boolean complete = FailureBoundary.cleanup(
                    () -> { steps[0]++; throw new IllegalArgumentException("TEST_UNREGISTER"); },
                    () -> { steps[0]++; throw new IllegalStateException("TEST_WAKE"); },
                    () -> steps[0]++);
            check(!complete && steps[0] == 3, "independent cleanup attempts");
        });
        test("stop remains disarmed if wake release and failure callback throw", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(4_000); r.throwRelease = true;
            boolean success = FailureBoundary.run(r.c::stop, () -> {
                FailureBoundary.cleanup(r.c::dispose, () -> { throw new IllegalStateException("TEST_NOTIFY"); });
                throw new IllegalStateException("TEST_FINAL_FAILURE");
            });
            check(!success && !r.c.getState().armed && r.queued == null, "stop invalidates before failing effect");
            r.set(0); r.advance(100_000); check(r.writes == 1, "no retry after failed cleanup");
        });
        System.out.println("ALL " + tests + " TESTS PASSED");
    }
}
