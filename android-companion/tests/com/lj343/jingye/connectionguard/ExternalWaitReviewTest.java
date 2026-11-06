package com.lj343.jingye.connectionguard;

import java.util.ArrayList;
import java.util.List;

/** Independent adversarial tests of the real policy; no Android APIs. */
public final class ExternalWaitReviewTest {
    private static int count;
    private static void check(boolean ok, String code) { if (!ok) throw new AssertionError(code); }
    private static final class Rig implements RecoveryController.Port, RecoveryController.Scheduler {
        boolean wifi = true, permission = true, failCommit, transientFalseWifiRead;
        int flag, writes;
        long now, due;
        Runnable queued;
        final List<Runnable> dispatched = new ArrayList<>();
        final RecoveryController policy;
        Rig() { this(0, new long[0]); }
        Rig(long attempts, long[] times) { policy = new RecoveryController(this, this, attempts, times); }
        public boolean wifiAvailable() {
            if (transientFalseWifiRead) { transientFalseWifiRead = false; return false; }
            return wifi;
        }
        public int readEnabled() { return flag; }
        public boolean canWrite() { return permission; }
        public boolean enableOnce() { writes++; flag = 1; return true; }
        public long nowMillis() { return now; }
        public void holdWake(long ms) { check(ms <= 25_000, "wake_budget"); }
        public void releaseWake() { }
        public void changed(RecoveryController.Snapshot s) {
            if (failCommit && "VERIFYING".equals(s.phase)) throw new IllegalStateException("SYNTHETIC_COMMIT_FAILED");
        }
        public void schedule(Runnable work, long delay) {
            check(queued == null, "one_pending_timer"); queued = work; due = now + delay; dispatched.add(work);
        }
        public void cancel(Runnable work) { if (queued == work) queued = null; }
        void advance(long delta) {
            long target = now + delta;
            while (queued != null && due <= target) {
                Runnable work = queued; queued = null; now = due; work.run();
            }
            now = target;
        }
        void on(int value) { flag = value; policy.settingChanged(); }
        boolean phase(String value) { return value.equals(policy.getState().phase); }
    }
    private static void test(Runnable work) { work.run(); count++; }

    public static void main(String[] args) {
        test(() -> {
            Rig r = new Rig(); r.flag = 1; r.policy.start(true); r.advance(29_999);
            // Caller observed false, then the current port recovered before a second read.
            r.policy.networkChanged(false);
            check(r.phase("WAIT_EXTERNAL_ON") && r.queued == null, "observed_loss_must_invalidate_old_stability");
            r.policy.networkChanged(true); r.advance(1);
            check(r.policy.getState().attemptConsumed, "old_deadline_must_not_rearm");
            r.advance(29_998); check(r.policy.getState().attemptConsumed, "new_full_interval_required");
            r.advance(1); check(r.phase("OPEN_PENDING_PC") && r.writes == 0, "passive_stability_only");
        });
        test(() -> {
            Rig r = new Rig(); r.flag = 1; r.policy.start(true); r.advance(29_999);
            Runnable stale = r.queued; r.policy.stop(); stale.run();
            r.policy.networkChanged(true); r.on(1); r.advance(120_000);
            check(r.phase("STOPPED") && !r.policy.getState().armed && r.queued == null && r.writes == 0,
                  "stop_never_revived_by_dispatched_timer");
        });
        test(() -> {
            Rig r = new Rig(); r.flag = 1; r.policy.start(true); r.advance(29_999);
            r.transientFalseWifiRead = true; r.advance(1);
            check(r.policy.getState().attemptConsumed && r.writes == 0, "final_false_read_cannot_rearm");
            check(r.phase("WAIT_EXTERNAL_ON") || (r.queued != null && r.due >= r.now + 30_000),
                  "deadline_loss_cannot_leave_stabilizing_without_timer");
            r.policy.networkChanged(true); r.advance(29_999);
            check(r.policy.getState().attemptConsumed && r.queued != null, "deadline_loss_requires_fresh_full_interval");
            r.advance(1); check(r.phase("OPEN_PENDING_PC") && r.writes == 0, "deadline_loss_full_interval_completed");
        });
        test(() -> {
            Rig before = new Rig(2, new long[]{1, 2}); before.flag = 1; before.policy.start(true); before.advance(29_999);
            RecoveryController.Snapshot saved = before.policy.getState(); before.policy.dispose();
            Rig after = new Rig(saved.attempts, saved.recentAttempts); after.now = before.now; after.flag = 1;
            after.policy.start(saved.attemptConsumed); after.advance(29_999);
            check(after.policy.getState().attemptConsumed && after.writes == 0, "recreation_cannot_restore_old_timer_credit");
            after.advance(1); check(after.phase("OPEN_PENDING_PC"), "recreated_full_stability");
            check(after.policy.getState().attempts == 2 && after.policy.getState().recentAttempts.length == 2,
                  "recreation_preserves_rate_history");
        });
        test(() -> {
            Rig r = new Rig(3, new long[]{Long.MAX_VALUE, Long.MAX_VALUE, Long.MAX_VALUE});
            r.flag = 1; r.policy.start(true); r.advance(30_000); r.on(0); r.advance(4_000);
            check(r.phase("PAUSED") && "RATE_LIMITED".equals(r.policy.getState().code) && r.writes == 0,
                  "external_stability_must_not_reset_corrupt_or_future_rate_history");
        });
        test(() -> {
            Rig r = new Rig(); r.flag = 1; r.policy.start(true); r.advance(29_999);
            r.flag = 0; r.advance(1); // Final read catches OFF even if the observer callback is delayed.
            check(r.phase("WAIT_EXTERNAL_ON") && r.policy.getState().attemptConsumed && r.queued == null,
                  "deadline_recheck_must_reject_unobserved_current_off");
            r.advance(1_000_000); check(r.writes == 0, "off_wait_cannot_poll_write");
        });
        test(() -> {
            Rig r = new Rig(); r.flag = 1; r.policy.start(true); r.advance(29_999); r.permission = false; r.advance(1);
            check(r.phase("PAUSED") && !r.policy.getState().armed, "permission_recheck_at_rearm");
            r.permission = true; r.on(1); r.policy.networkChanged(true); r.advance(1_000_000);
            check(!r.policy.getState().armed && r.writes == 0 && r.queued == null, "permission_pause_not_auto_rearmed");
        });
        test(() -> {
            Rig r = new Rig(); r.policy.start(false); r.failCommit = true;
            boolean ok = FailureBoundary.run(() -> r.advance(4_000), r.policy::dispose);
            check(!ok && !r.policy.getState().armed && r.writes == 0, "persist_failure_precedes_first_write");
            r.failCommit = false; // Even if storage recovers, unsolicited events cannot rearm.
            r.on(1); r.policy.networkChanged(true); r.advance(1_000_000);
            check(r.writes == 0 && !r.policy.getState().armed, "failed_commit_never_auto_revived");
        });
        test(() -> {
            Rig r = new Rig(2, new long[]{10, 20}); r.policy.start(true);
            for (int i = 0; i < 1_000; i++) {
                r.wifi = false; r.policy.networkChanged(false); r.wifi = true; r.policy.networkChanged(true); r.on(0);
            }
            r.advance(1_000_000);
            check(r.writes == 0 && r.policy.getState().attemptConsumed && r.queued == null,
                  "wifi_flood_cannot_replenish_consumed_off_episode");
            check(r.policy.getState().recentAttempts.length == 2, "event_flood_does_not_reset_rate");
        });
        System.out.println("INDEPENDENT " + count + " TESTS PASSED");
    }
}
