package com.lj343.jingye.connectionguard;

/** Actual approval coordinator, independent of Android so adverse paths can be tested. */
final class TrustGuard {
    static final String OWNER="com.android.systemui";
    static final String ACTIVITY=OWNER+".wifi.WifiDebuggingActivity";
    static final String ALIAS=OWNER+".WifiDebuggingActivityAlias";
    static final String EXISTING_SYSTEM_DIALOG="EXACT_SYSTEM_DIALOG_SHAPE";
    enum Action { CHECK_ALWAYS, ALLOW }
    static final class Frame {
        boolean enabled, armed, unlocked, platformSigned, visible, allowlisted, controlsValid, checked, systemWindow;
        boolean applicationWindow, honorSplitDialog;
        String owner, activity, title, message, allow, always, cancel;
        String expectedTitle, expectedMessage, expectedAllow, expectedAlways, expectedCancel;
        TrustIdentity identity;
        int window=-1;
        boolean sameTarget(Frame b) {
            return b!=null && identity!=null && b.identity!=null && identity.key.equals(b.identity.key)
                    && window==b.window && checked==b.checked && eq(activity,b.activity)
                    && honorSplitDialog==b.honorSplitDialog && applicationWindow==b.applicationWindow
                    && systemWindow==b.systemWindow;
        }
    }
    interface Port {
        Frame inspect();
        boolean reserve(Action action, String identityKey, int window);
        boolean click(Action action, Frame expected);
        void record(String code);
    }
    private final Port port;
    TrustGuard(Port p) { port=p; }
    static boolean eq(String a,String b) { return a!=null && a.equals(b); }
    static String block(Frame f) {
        if (f==null) return "NO_MATCHING_DIALOG";
        if (!f.enabled || !f.armed) return "TRUST_DISABLED_OR_GUARD_STOPPED";
        if (!f.unlocked) return "SCREEN_LOCKED_OR_OFF";
        boolean route=ACTIVITY.equals(f.activity) || ALIAS.equals(f.activity)
                || (EXISTING_SYSTEM_DIALOG.equals(f.activity)
                    && (f.systemWindow || (f.applicationWindow && f.honorSplitDialog)));
        if (!f.platformSigned || !OWNER.equals(f.owner) || !route) return "WINDOW_OWNER_MISMATCH";
        if (f.identity==null) return "NETWORK_IDENTITY_UNAVAILABLE";
        if (!f.allowlisted) return "AP_NOT_PREAUTHORIZED";
        if (!f.visible || f.window<0 || !f.controlsValid) return "DIALOG_STRUCTURE_UNRECOGNIZED";
        if (!eq(f.title,f.expectedTitle) || !eq(f.message,f.expectedMessage)
                || !eq(f.allow,f.expectedAllow) || !eq(f.always,f.expectedAlways)
                || !eq(f.cancel,f.expectedCancel)) return "DIALOG_CONTENT_UNRECOGNIZED";
        return null;
    }
    void event() {
        try {
            Frame first=port.inspect(); String reason=block(first);
            if (reason!=null) { port.record(reason); return; }
            Action action=first.checked?Action.ALLOW:Action.CHECK_ALWAYS;
            // Persist the allowance before any UI action. False includes duplicate/rate/storage rejection.
            if (!port.reserve(action,first.identity.key,first.window)) { port.record("TRUST_ACTION_BLOCKED"); return; }
            Frame fresh=port.inspect();
            if (block(fresh)!=null || !first.sameTarget(fresh)) { port.record("DIALOG_OR_NETWORK_CHANGED"); return; }
            boolean sent=port.click(action,fresh);
            port.record(sent ? (action==Action.ALLOW?"ALLOW_CLICK_SENT_PC_UNVERIFIED":"ALWAYS_CLICK_SENT") : "SYSTEM_CLICK_REJECTED");
        } catch (RuntimeException fault) { port.record("TRUST_PLATFORM_EXCEPTION"); }
    }
}
