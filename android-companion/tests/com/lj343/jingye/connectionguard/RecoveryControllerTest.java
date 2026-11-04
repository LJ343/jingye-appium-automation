package com.lj343.jingye.connectionguard;

import java.util.ArrayList;
import java.util.List;

/** Deterministic adversarial event tests; uses the production policy, no Android runtime. */
public final class RecoveryControllerTest {
    private static int tests;
    private static final class Rig implements RecoveryController.Port, RecoveryController.Scheduler {
        boolean wifi = true, permission = true, acceptWrite = true, latchWrite = true;
        boolean throwWrite, throwRelease, nextReadOff;
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
        public int readEnabled() { if (nextReadOff) { nextReadOff = false; return 0; } return flag; }
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
        test("framework immediate reset stays read only and never loops", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(4_000); r.set(0); phase(r, "WAIT_EXTERNAL_ON");
            for (int i = 0; i < 100; i++) { r.set(0); r.net(false); r.net(true); r.advance(20_000); }
            check(r.writes == 1 && !r.wake && r.queued == null && r.c.getState().armed
                    && r.c.getState().attemptConsumed, "no rejection loop or replenished budget");
        });
        test("a write that never latches waits read only at deadline", () -> {
            Rig r = new Rig(); r.latchWrite = false; r.c.start(false); r.advance(16_000);
            phase(r, "WAIT_EXTERNAL_ON"); check(r.writes == 1 && !r.wake, "bounded failed write");
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
        test("reset during thirty-second cooldown waits read only", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(16_000); phase(r, "COOLDOWN");
            r.set(0); r.advance(100_000); phase(r, "WAIT_EXTERNAL_ON"); check(r.writes == 1, "early second off cannot write again");
        });
        test("manual resume explicitly replenishes attempt", () -> {
            Rig r = new Rig(); r.latchWrite = false; r.c.start(false); r.advance(16_000); phase(r, "WAIT_EXTERNAL_ON");
            r.latchWrite = true; r.c.start(false); r.advance(34_000); check(r.writes == 2, "explicit second attempt"); phase(r, "OPEN_PENDING_PC");
        });
        test("sticky recreation keeps consumed attempt budget", () -> {
            Rig r = new Rig(); r.c.start(true); r.advance(100_000);
            phase(r, "WAIT_EXTERNAL_ON"); check(r.writes == 0, "restored consumed budget cannot write");
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
            Rig r = new Rig(); r.flag = 1; r.c.start(true); phase(r, "EXTERNAL_ON_STABILIZING");
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
            r.set(1); r.advance(100_000); check(!r.c.getState().armed && r.writes == 1, "external ON cannot revive platform failure");
        });
        test("early reset then external ON rearms only after thirty seconds without writing", () -> {
            Rig r = new Rig(); r.c.start(false); r.advance(4_000); r.set(0);
            long attempts = r.c.getState().attempts;
            r.set(1); phase(r, "EXTERNAL_ON_STABILIZING"); r.advance(29_999);
            check(r.c.getState().attemptConsumed && r.writes == 1, "no early rearm or write");
            r.advance(1); phase(r, "OPEN_PENDING_PC");
            check(!r.c.getState().attemptConsumed && r.c.getState().attempts == attempts && r.writes == 1,
                    "external recovery is read only");
            r.set(0); r.advance(4_000); check(r.writes == 2, "later independent episode may recover");
        });
        test("external stable interval resets on OFF or real Wi-Fi loss", () -> {
            Rig r = new Rig(); r.c.start(true); r.set(1); r.advance(20_000);
            r.set(0); r.advance(100_000); phase(r, "WAIT_EXTERNAL_ON");
            r.set(1); r.advance(20_000); r.net(false); r.advance(100_000);
            phase(r, "WAIT_EXTERNAL_ON"); check(r.c.getState().attemptConsumed && r.writes == 0, "loss preserves consumed budget");
            r.net(true); r.advance(29_999); check(r.c.getState().attemptConsumed, "new full interval required");
            r.advance(1); phase(r, "OPEN_PENDING_PC"); check(r.writes == 0, "network return never writes while waiting");
        });
        test("duplicate external ON events neither multiply nor postpone stable timer", () -> {
            Rig r = new Rig(); r.c.start(true); r.set(1);
            for (int i = 0; i < 29; i++) { r.advance(1_000); r.set(1); r.net(true); }
            r.advance(1_000); phase(r, "OPEN_PENDING_PC");
            check(r.writes == 0 && r.queued == null && r.maxPending == 1, "one unchanged stable timer");
        });
        test("observed network loss cancels stable timer even if Wi-Fi already returned", () -> {
            Rig r = new Rig(); r.flag = 1; r.c.start(true); r.advance(29_999);
            r.c.networkChanged(false); phase(r, "WAIT_EXTERNAL_ON");
            check(r.queued == null && r.c.getState().attemptConsumed, "false event cannot be erased by new availability");
            r.c.networkChanged(true); r.advance(29_999); check(r.c.getState().attemptConsumed, "fresh thirty seconds required");
            r.advance(1); phase(r, "OPEN_PENDING_PC"); check(r.writes == 0, "race recovery stays read only");
        });
        test("observed switch OFF cancels stable timer even if next read would be ON", () -> {
            Rig r = new Rig(); r.flag = 1; r.c.start(true); r.advance(29_999);
            r.nextReadOff = true; r.c.settingChanged(); phase(r, "WAIT_EXTERNAL_ON");
            check(r.queued == null && r.c.getState().attemptConsumed, "observed OFF cannot be reread away");
            r.c.settingChanged(); r.advance(29_999); check(r.c.getState().attemptConsumed, "new full interval after ON");
            r.advance(1); phase(r, "OPEN_PENDING_PC"); check(r.writes == 0, "setting race recovery stays read only");
        });
        test("OFF then ON at stable deadline starts a fresh timer instead of getting stuck", () -> {
            Rig r = new Rig(); r.flag = 1; r.c.start(true); r.nextReadOff = true;
            r.advance(30_000); phase(r, "EXTERNAL_ON_STABILIZING");
            check(r.queued != null && r.c.getState().attemptConsumed, "a consumed callback cannot leave a timerless stabilizing state");
            r.advance(29_999); check(r.c.getState().attemptConsumed, "deadline reset requires full new interval");
            r.advance(1); phase(r, "OPEN_PENDING_PC"); check(r.writes == 0, "deadline race never writes");
        });
        test("STOP during external observation cancels stale timers permanently", () -> {
            Rig r = new Rig(); r.c.start(true); r.set(1); Runnable stale = r.queued;
            r.c.stop(); stale.run(); r.net(false); r.net(true); r.set(1); r.advance(100_000);
            phase(r, "STOPPED"); check(!r.c.getState().armed && r.writes == 0 && r.queued == null, "user stop cannot auto revive");
        });
        test("hard permission write and rate pauses cannot auto revive on external ON", () -> {
            Rig missing = new Rig(); missing.permission = false; missing.c.start(false);
            missing.permission = true; missing.set(1); missing.advance(100_000);
            phase(missing, "PAUSED"); check(!missing.c.getState().armed && missing.writes == 0, "permission pause stays stopped");
            Rig rejected = new Rig(); rejected.acceptWrite = false; rejected.c.start(false); rejected.advance(4_000);
            rejected.set(1); rejected.advance(100_000); phase(rejected, "PAUSED");
            check(!rejected.c.getState().armed && rejected.writes == 1, "write rejection stays stopped");
            Rig rate = new Rig(3, new long[]{0, 1, 2}); rate.now = 100; rate.c.start(false); rate.advance(4_000);
            rate.set(1); rate.advance(100_000); phase(rate, "PAUSED");
            check(!rate.c.getState().armed && rate.writes == 0 && "RATE_LIMITED".equals(rate.c.getState().code), "rate pause stays stopped");
        });
        test("service recreation restores passive wait and does not reset persistent rate history", () -> {
            Rig before = new Rig(3, new long[]{0, 1, 2}); before.now = 100; before.c.start(true);
            RecoveryController.Snapshot snapshot = before.c.getState(); before.c.dispose();
            Rig restored = new Rig(snapshot.attempts, snapshot.recentAttempts); restored.now = before.now;
            restored.c.start(snapshot.attemptConsumed); restored.net(false); restored.net(true); restored.advance(100_000);
            phase(restored, "WAIT_EXTERNAL_ON"); check(restored.writes == 0 && restored.c.getState().attemptConsumed, "restoration does not retry");
            restored.set(1); restored.advance(30_000); phase(restored, "OPEN_PENDING_PC");
            check(restored.c.getState().attempts == 3 && restored.c.getState().recentAttempts.length == 3, "rate history preserved");
            restored.set(0); restored.advance(4_000); phase(restored, "PAUSED");
            check(restored.writes == 0 && "RATE_LIMITED".equals(restored.c.getState().code), "new episode still rate limited");
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
