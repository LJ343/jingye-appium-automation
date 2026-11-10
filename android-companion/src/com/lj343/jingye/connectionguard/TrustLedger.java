package com.lj343.jingye.connectionguard;

import java.util.ArrayList;
import java.util.List;

/** Persisted per-action budget: at most three checkbox and three allow actions per ten minutes. */
final class TrustLedger {
    static final long WINDOW_MS=600_000L;
    static final int LIMIT=3;
    static final class Row {
        long time; String action,key; int window;
        Row(long t,String a,String k,int w) { time=t;action=a;key=k;window=w; }
    }
    static String reserve(String raw,TrustGuard.Action action,String key,int window,long now) {
        if (now<=0 || key==null || !key.matches("[0-9a-f]{64}") || window<0) return null;
        List<Row> rows=new ArrayList<>(); int count=0;
        if (raw==null || raw.length()>4096) return null;
        try {
            if (!raw.isEmpty()) for (String line:raw.split("\n",-1)) {
                String[] f=line.split(",",-1); if (f.length!=4) return null;
                long t=Long.parseLong(f[0]); int w=Integer.parseInt(f[3]);
                if (t<=0 || t>now || w<0 || !f[2].matches("[0-9a-f]{64}")) return null;
                TrustGuard.Action.valueOf(f[1]);
                if (now-t<WINDOW_MS) {
                    rows.add(new Row(t,f[1],f[2],w));
                    if (f[1].equals(action.name())) {
                        count++;
                        if (f[2].equals(key) && w==window) return null;
                    }
                }
            }
        } catch (IllegalArgumentException invalid) { return null; }
        if (count>=LIMIT || rows.size()>=2*LIMIT) return null;
        rows.add(new Row(now,action.name(),key,window));
        StringBuilder out=new StringBuilder();
        for(Row r:rows) {
            if(out.length()>0)out.append('\n');
            out.append(r.time).append(',').append(r.action).append(',').append(r.key).append(',').append(r.window);
        }
        return out.toString();
    }
}
