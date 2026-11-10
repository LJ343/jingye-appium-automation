package com.lj343.jingye.connectionguard;

/** Bounded inspection burst keyed by the actual focused window, never an event's window id. */
final class TrustWindowSession {
    private int window=-1, checks;
    private long until;
    boolean observe(int focusedWindow,long now) {
        if(focusedWindow<0)return false;
        if(window!=focusedWindow) {
            // Replacement windows within a burst share its remaining time and checks.
            if(window<0 || now>until) { checks=0;until=now+8_000L; }
            window=focusedWindow;
        }
        return available(now);
    }
    boolean available(long now) { return window>=0 && checks<6 && now<=until; }
    boolean current(long now) { return window>=0 && now<=until; }
    boolean take(long now) { if(!available(now))return false;checks++;return true; }
    void clear() { window=-1;checks=0;until=0; }
}
