package com.lj343.jingye.connectionguard;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;

/** Shell DUMP permission permits observation only. Query never starts recovery. */
public final class StatusProvider extends ContentProvider {
    private static final String AUTHORITY = "com.lj343.jingye.connectionguard.status";
    @Override public boolean onCreate() { return true; }
    @Override public Cursor query(Uri uri, String[] projection, String selection,
                                  String[] selectionArgs, String sortOrder) {
        getContext().enforceCallingOrSelfPermission("android.permission.DUMP", "Read-only diagnostics require DUMP");
        if (!AUTHORITY.equals(uri.getAuthority()) || !"/status".equals(uri.getPath())
                || projection != null || selection != null || selectionArgs != null || sortOrder != null
                || uri.getQuery() != null) throw new IllegalArgumentException("Only plain /status query is supported");
        MatrixCursor cursor = new MatrixCursor(new String[]{"status"}, 1);
        String state;
        try { state = new StateStore(getContext()).snapshot(); }
        catch (RuntimeException failure) { state = "{\"code\":\"DIAGNOSTIC_UNAVAILABLE\",\"record_write_result\":\"UNKNOWN\"}"; }
        cursor.addRow(new Object[]{state});
        return cursor;
    }
    @Override public String getType(Uri uri) { return "vnd.android.cursor.item/vnd.lj343.connectionguard.status"; }
    @Override public Uri insert(Uri uri, ContentValues values) { throw new UnsupportedOperationException("Read only"); }
    @Override public int update(Uri uri, ContentValues v, String s, String[] args) { throw new UnsupportedOperationException("Read only"); }
    @Override public int delete(Uri uri, String s, String[] args) { throw new UnsupportedOperationException("Read only"); }
}
