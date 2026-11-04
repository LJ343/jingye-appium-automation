package com.lj343.jingye.connectionguard;

/** Runtime-only platform boundary; never records exception text or retries failed work. */
final class FailureBoundary {
    private FailureBoundary() { }
    static boolean run(Runnable work, Runnable onFailure) {
        try { work.run(); return true; }
        catch (RuntimeException platformFailure) {
            // The failure handler is itself a best-effort teardown, never a retry.
            try { onFailure.run(); } catch (RuntimeException cleanupFailure) { }
            return false;
        }
    }
    static boolean cleanup(Runnable... steps) {
        boolean complete = true;
        for (Runnable step : steps) {
            try { step.run(); } catch (RuntimeException cleanupFailure) { complete = false; }
        }
        return complete;
    }
}
