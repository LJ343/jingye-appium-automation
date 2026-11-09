package com.lj343.jingye.connectionguard;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.provider.Settings;
import android.view.View;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;
import org.json.JSONObject;

public final class MainActivity extends Activity {
    private static final int NOTIFICATION_REQUEST = 4;
    private TextView stateView;
    private StateStore store;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved); store = new StateStore(this);
        LinearLayout layout = new LinearLayout(this); layout.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (20 * getResources().getDisplayMetrics().density);
        layout.setPadding(pad, pad, pad, pad);
        TextView title = new TextView(this); title.setText("敬业连接恢复助手 · 0.4"); title.setTextSize(23); layout.addView(title);
        TextView explanation = new TextView(this);
        explanation.setText("默认不启用。只有您点击开始后，助手才会尝试恢复无线调试。\n\n"
                + "启用期间，系统关闭无线调试后可能被恢复；要手动保持关闭，请先暂停并停止助手。"
                + "遇到未信任网络仍需系统确认，助手不自动批准。\n\n"
                + "Wi-Fi 稳定后尝试恢复。开启未保持时，退避 60 秒、120 秒最多再试两次；"
                + "本轮最多三次写入，重建服务不会补充预算或跳过退避。连续失败后停止写入并显示原因。"
                + "开关持续开启 30 秒后才接受下一次独立故障；外部开启也须稳定 30 秒。"
                + "按系统时间计十分钟达到三次恢复，或权限/系统接口失败，会暂停，须您主动恢复。"
                + "每次恢复最多持有 25 秒唤醒锁。\n\n"
                + "需要另行批准 WRITE_SECURE_SETTINGS 开发权限；本应用不会自行授予。"
                + "不开机自启，系统仍可能结束后台进程，不能保证永久在线。\n\n"
                + "地址上报须单独配置。配置后，仅在助手启用且无线调试开启时，每 15 秒向指定电脑发送一次本机 Wi-Fi 地址认证报文；"
                + "不上传业务资料。电脑仅在等待连接时接收；收到地址后仍需发现端口并核验手机身份。地址上报不代表电脑已连接。\n");
        layout.addView(explanation);
        addButton(layout, "开始 / 恢复一次", v -> requestStart());
        addButton(layout, "暂停并停止助手", v -> FailureBoundary.run(() -> {
            startService(new Intent(this, GuardService.class).setAction(GuardService.ACTION_STOP));
            showToast("已发送停止指令；点击刷新核实");
        }, this::operationFailed));
        addButton(layout, "刷新诊断状态", v -> refresh());
        addButton(layout, "复制诊断状态", v -> FailureBoundary.run(() -> {
            getSystemService(ClipboardManager.class).setPrimaryClip(ClipData.newPlainText("连接恢复诊断", diagnostic()));
            showToast("已复制，不会发送网络");
        }, this::operationFailed));
        addButton(layout, "系统应用设置", v -> FailureBoundary.run(() -> startActivity(new Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                Uri.parse("package:" + getPackageName()))), this::operationFailed));
        stateView = new TextView(this); stateView.setTextIsSelectable(true); layout.addView(stateView);
        ScrollView scroll = new ScrollView(this); scroll.addView(layout); setContentView(scroll);
    }

    private void addButton(LinearLayout layout, String text, View.OnClickListener click) {
        Button button = new Button(this); button.setText(text); button.setOnClickListener(click); layout.addView(button);
    }

    private void requestStart() {
        FailureBoundary.run(this::performStart, this::operationFailed);
    }
    private void performStart() {
        if (checkSelfPermission(Manifest.permission.WRITE_SECURE_SETTINGS) != PackageManager.PERMISSION_GRANTED) {
            showToast("尚未批准系统写入权限；不会启用无线调试"); return;
        }
        if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_REQUEST); return;
        }
        startForegroundService(new Intent(this, GuardService.class).setAction(GuardService.ACTION_START));
        showToast("已发送开始指令；点击刷新核实");
    }

    @Override public void onRequestPermissionsResult(int request, String[] permissions, int[] grants) {
        super.onRequestPermissionsResult(request, permissions, grants);
        if (request == NOTIFICATION_REQUEST && grants.length > 0 && grants[0] == PackageManager.PERMISSION_GRANTED) requestStart();
        else if (request == NOTIFICATION_REQUEST) showToast("未允许通知，未发送开始指令");
    }
    @Override public void onResume() { super.onResume(); refresh(); }
    private void refresh() { FailureBoundary.run(() -> { if (stateView != null) stateView.setText(diagnostic()); }, this::operationFailed); }
    private void showToast(String text) { FailureBoundary.cleanup(() -> Toast.makeText(this, text, Toast.LENGTH_LONG).show()); }
    private void operationFailed() { showToast("系统拒绝操作或状态未确认；请刷新诊断，勿按成功处理"); }
    private String diagnostic() {
        String snapshot = store.snapshot();
        String code = "USER_STOPPED";
        try { code = new JSONObject(snapshot).optString("code", code); } catch (Exception ignored) { }
        String reporting = "地址上报配置未确认";
        try { reporting = ReporterSettings.load(this) == null ? "地址上报尚未配置" : "地址上报已配置；电脑连接仍需单独核验"; }
        catch (RuntimeException ignored) { }
        return GuardService.statusText(code) + "\n\n以下为最近上报，系统强制结束后可能过时；不代表电脑已连接。\n"
                + snapshot + "\n\n" + reporting + "\n\n最近状态（最多 40 条）\n" + store.history();
    }
}
