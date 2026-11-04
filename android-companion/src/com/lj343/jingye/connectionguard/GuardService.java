package com.lj343.jingye.connectionguard;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.pm.ServiceInfo;
import android.database.ContentObserver;
import android.net.ConnectivityManager;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.net.NetworkRequest;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.os.Build;
import android.os.PowerManager;
import android.provider.Settings;

/** Every controller invocation and setting write runs on the main looper. */
public final class GuardService extends Service implements RecoveryController.Port {
    public static final String ACTION_START = "com.lj343.jingye.connectionguard.START";
    public static final String ACTION_STOP = "com.lj343.jingye.connectionguard.STOP";
    private static final String KEY = "adb_wifi_enabled";
    private static final String CHANNEL = "connection_recovery";
    private static final int NOTIFICATION_ID = 7304;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private ConnectivityManager connectivity;
    private RecoveryController controller;
    private StateStore store;
    private PowerManager.WakeLock wakeLock;
    private boolean watching, observerAttempted, networkAttempted, foreground, lastStoreOk = true;
    private boolean fatal, destroying;

    private final ConnectivityManager.NetworkCallback networks = new ConnectivityManager.NetworkCallback() {
        @Override public void onAvailable(Network network) { refreshNetwork(); }
        @Override public void onLost(Network network) { refreshNetwork(); }
        @Override public void onCapabilitiesChanged(Network n, NetworkCapabilities caps) { refreshNetwork(); }
    };
    private final ContentObserver setting = new ContentObserver(handler) {
        @Override public void onChange(boolean selfChange) {
            guarded(() -> { if (watching) controller.settingChanged(); });
        }
    };

    @Override public void onCreate() {
        super.onCreate();
        guarded(() -> {
            store = new StateStore(this);
            connectivity = getSystemService(ConnectivityManager.class);
            wakeLock = getSystemService(PowerManager.class).newWakeLock(PowerManager.PARTIAL_WAKE_LOCK,
                    "JingyeConnectionGuard:bounded-recovery");
            wakeLock.setReferenceCounted(false);
            controller = new RecoveryController(this, new RecoveryController.Scheduler() {
                private Runnable original, wrapped;
                public void schedule(Runnable work, long delay) {
                    original = work; wrapped = () -> guarded(work);
                    if (!handler.postDelayed(wrapped, delay)) throw new IllegalStateException("SCHEDULER_REJECTED");
                }
                public void cancel(Runnable work) {
                    if (original == work && wrapped != null) handler.removeCallbacks(wrapped);
                    original = null; wrapped = null;
                }
            }, store.attempts(), store.recentAttempts());
            getSystemService(NotificationManager.class).createNotificationChannel(new NotificationChannel(
                    CHANNEL, "连接恢复状态", NotificationManager.IMPORTANCE_LOW));
        });
    }

    @Override public int onStartCommand(Intent intent, int flags, int startId) {
        final int[] result = {START_NOT_STICKY};
        guarded(() -> {
            handleStart(intent);
            if (!fatal && !destroying && controller != null && controller.getState().armed) result[0] = START_STICKY;
        });
        return fatal ? START_NOT_STICKY : result[0];
    }

    private void handleStart(Intent intent) {
        String action = intent == null ? null : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            controller.stop();
            if (!unwatchBestEffort()) throw new IllegalStateException("WATCH_CLEANUP_FAILED");
            stopForeground(STOP_FOREGROUND_REMOVE); foreground = false; stopSelf();
            return;
        }
        boolean explicitStart = ACTION_START.equals(action);
        if (!explicitStart && (intent != null || !store.desiredArmed())) {
            stopSelf(); return;
        }
        // Only the visible activity can start this unexported service. Sticky recreation
        // retains the previously persisted budget rather than issuing a fresh attempt.
        foreground = true;
        startForeground(NOTIFICATION_ID, notification("正在准备连接恢复"),
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE);
        watch();
        controller.start(!explicitStart && store.consumed());
    }

    private void watch() {
        if (watching) return;
        observerAttempted = true;
        getContentResolver().registerContentObserver(Settings.Global.getUriFor(KEY), false, setting);
        NetworkRequest request = new NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI)
                .removeCapability(NetworkCapabilities.NET_CAPABILITY_NOT_RESTRICTED).build();
        networkAttempted = true;
        connectivity.registerNetworkCallback(request, networks, handler);
        watching = true;
    }

    private boolean unwatchBestEffort() {
        watching = false;
        boolean observer = observerAttempted, network = networkAttempted;
        observerAttempted = false; networkAttempted = false;
        return FailureBoundary.cleanup(
                () -> { if (observer) getContentResolver().unregisterContentObserver(setting); },
                () -> { if (network && connectivity != null) connectivity.unregisterNetworkCallback(networks); });
    }

    private void refreshNetwork() { guarded(() -> { if (watching) controller.networkChanged(wifiAvailable()); }); }

    private void guarded(Runnable work) {
        if (!fatal && !destroying) FailureBoundary.run(work, this::failClosed);
    }

    /** No retry and no setting write is allowed after entering this method. */
    private void failClosed() {
        if (fatal) return;
        fatal = true; lastStoreOk = false;
        StateStore.markPlatformFault();
        FailureBoundary.cleanup(
                // Persist revoked recovery intent before cancellation or notification APIs.
                () -> { if (store != null) store.platformFailure(); },
                () -> { if (controller != null) controller.dispose(); },
                () -> handler.removeCallbacksAndMessages(null),
                this::unwatchBestEffort,
                this::releaseWake,
                () -> { if (foreground) stopForeground(STOP_FOREGROUND_REMOVE); },
                () -> {
                    foreground = false;
                    Notification error = notification(statusText("PLATFORM_EXCEPTION"));
                    error.flags &= ~Notification.FLAG_ONGOING_EVENT;
                    getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, error);
                },
                this::stopSelf);
    }

    @Override public boolean wifiAvailable() {
        for (Network n : connectivity.getAllNetworks()) {
            NetworkCapabilities c = connectivity.getNetworkCapabilities(n);
            if (c != null && c.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) return true;
        }
        return false;
    }
    @Override public int readEnabled() { return Settings.Global.getInt(getContentResolver(), KEY, 0); }
    @Override public long nowMillis() { return System.currentTimeMillis(); }
    @Override public boolean canWrite() {
        return !fatal && !destroying && lastStoreOk && checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) == PackageManager.PERMISSION_GRANTED
                && (Build.VERSION.SDK_INT < 33 || checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) == PackageManager.PERMISSION_GRANTED);
    }
    @Override public boolean enableOnce() {
        if (fatal || destroying || !controller.getState().armed || !lastStoreOk || !canWrite()) return false;
        return Settings.Global.putInt(getContentResolver(), KEY, 1);
    }
    @Override public void holdWake(long timeout) { wakeLock.acquire(Math.min(timeout, RecoveryController.WAKE_WINDOW_MS)); }
    @Override public void releaseWake() { if (wakeLock != null && wakeLock.isHeld()) wakeLock.release(); }

    @Override public void changed(RecoveryController.Snapshot state) {
        if (fatal || destroying) return;
        lastStoreOk = store.save(state, true);
        if (!lastStoreOk) throw new IllegalStateException("STATE_PERSIST_FAILED");
        if (foreground) getSystemService(NotificationManager.class).notify(NOTIFICATION_ID, notification(statusText(state.code)));
        if (!state.armed && !unwatchBestEffort()) throw new IllegalStateException("WATCH_CLEANUP_FAILED");
    }

    private Notification notification(String text) {
        PendingIntent open = PendingIntent.getActivity(this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        PendingIntent stop = PendingIntent.getService(this, 1, new Intent(this, GuardService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.stat_sys_data_bluetooth)
                .setContentTitle("敬业连接恢复助手").setContentText(text).setStyle(new Notification.BigTextStyle().bigText(text))
                .setContentIntent(open).addAction(new Notification.Action.Builder(null, "暂停并停止助手", stop).build())
                .setOngoing(true).setOnlyAlertOnce(true).setCategory(Notification.CATEGORY_SERVICE).build();
    }

    static String statusText(String code) {
        switch (code) {
            case "WAIT_WIFI": return "等待 Wi-Fi；网络恢复后再尝试";
            case "WAIT_STABLE_WIFI": return "等待网络稳定，仅恢复一次";
            case "VERIFY_SWITCH_ONLY": return "已尝试恢复，正在观察系统开关";
            case "SWITCH_ON_PC_UNVERIFIED": return "系统开关已开启，电脑连接待验证";
            case "SYSTEM_DID_NOT_KEEP_ON": return "系统未保持开启，可能需确认网络；不自动批准";
            case "PERMISSION_MISSING": return "所需写入或通知权限不可用，已暂停";
            case "SETTING_WRITE_REJECTED": return "系统拒绝设置写入，已暂停";
            case "RATE_LIMITED": return "十分钟恢复已达三次，已暂停；请检查网络后手动恢复";
            case "PLATFORM_EXCEPTION": return "系统接口异常，助手已暂停；请人工检查";
            default: return "助手已停止，不会恢复无线调试";
        }
    }

    @Override public void onDestroy() {
        destroying = true;
        boolean complete = FailureBoundary.cleanup(
                () -> { if (controller != null) controller.dispose(); },
                () -> handler.removeCallbacksAndMessages(null),
                () -> { if (!unwatchBestEffort()) throw new IllegalStateException("WATCH_CLEANUP_FAILED"); },
                this::releaseWake,
                () -> { if (store != null) { if (fatal) store.platformFailure(); else store.serviceGone(); } });
        if (!complete) FailureBoundary.cleanup(() -> { if (store != null) store.platformFailure(); });
        super.onDestroy();
    }
    @Override public IBinder onBind(Intent intent) { return null; }
}
