package com.lj343.jingye.connectionguard;

/** Reserve before action, no refill from events or an uncertain platform result. */
final class PairUiPolicy {
    private int scrolls;private long next;
    boolean reserveScroll(boolean official,boolean developer,boolean rowVisible,int lists,long now){
        if(!official || !developer || rowVisible || lists!=1 || now<0 || now<next || scrolls>=6)return false;
        scrolls++;next=now+700;return true;
    }
    int used(){return scrolls;}
}
