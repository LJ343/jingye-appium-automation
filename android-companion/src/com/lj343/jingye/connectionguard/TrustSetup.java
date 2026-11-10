package com.lj343.jingye.connectionguard;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.provider.Settings;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.Toast;

/** Foreground-only, explicit enrollment. Nothing imports APs from scans, logs or SSID patterns. */
final class TrustSetup {
    static final int LOCATION_REQUEST=21;
    private final Activity activity;
    private final TrustSettings settings;
    TrustSetup(Activity a) { activity=a;settings=new TrustSettings(a); }
    void addButtons(LinearLayout layout) {
        button(layout,"登记当前接入点",this::enroll);
        button(layout,"启用白名单自动确认",()->{
            if(settings.count()==0) { toast("请先登记您认可的当前接入点");return; }
            new AlertDialog.Builder(activity).setTitle("启用已授权接入点自动确认？")
                    .setMessage("仅在助手运行、手机已解锁且系统无线调试授权框中的接入点与白名单一致时操作。"
                            +"未知接入点、无法识别的系统界面仍需您决定。每十分钟最多三次授权点击，服务重建不重置。"
                            +"需要您另外在系统设置中启用“敬业：已授权接入点确认”辅助功能。"
                            +"辅助功能只监听系统界面，不读取或操作敬业业务界面；不会自行解锁手机。")
                    .setNegativeButton("取消",null).setPositiveButton("启用",(d,w)->safe(()->{
                        if(!settings.enable(true)) {toast("未成功保存，功能未确认启用");return;}
                        settings.record("ENABLED_AWAITING_SYSTEM_ACCESSIBILITY");
                        accessibility();
                    })).show();
        });
        button(layout,"打开辅助功能设置",this::accessibility);
        button(layout,"关闭接入点自动确认",()->toast(settings.enable(false)?"已关闭自动确认；原连接恢复功能保留":"保存失败，请检查诊断"));
        button(layout,"清空接入点白名单",()->new AlertDialog.Builder(activity).setTitle("清空接入点白名单？")
                .setMessage("同时关闭自动确认，已有系统网络信任不变。操作次数记录会保留。")
                .setNegativeButton("取消",null).setPositiveButton("清空",(d,w)->safe(()->toast(settings.clear()?"已清空并关闭自动确认":"保存失败，请检查诊断"))).show());
    }
    private void enroll() {
        if(activity.checkSelfPermission(Manifest.permission.ACCESS_FINE_LOCATION)!=PackageManager.PERMISSION_GRANTED) {
            new AlertDialog.Builder(activity).setTitle("登记接入点需要读取 Wi-Fi 身份")
                    .setMessage("Android把 Wi-Fi 接入点标识归入位置权限。仅登记时读取当前 Wi-Fi 名称与接入点标识，"
                            +"不请求后台定位、不读取GPS坐标。请选择精确位置；系统定位开关也须开启。授权后再次点击登记。")
                    .setNegativeButton("取消",null).setPositiveButton("继续",(d,w)->activity.requestPermissions(
                            new String[]{Manifest.permission.ACCESS_FINE_LOCATION,Manifest.permission.ACCESS_COARSE_LOCATION},LOCATION_REQUEST)).show();
            return;
        }
        TrustIdentity id=TrustWifi.read(activity);
        if(id==null) {toast("无法确认当前接入点；请检查 Wi-Fi、位置权限和系统定位开关，未登记");return;}
        new AlertDialog.Builder(activity).setTitle("允许在这个接入点恢复无线调试？")
                .setMessage("Wi-Fi："+id.ssid+"\n接入点："+id.bssid+"\n\n仅登记这一个接入点，同名网络的其他接入点不会自动加入。"
                        +"本机只保存其组合摘要，不发送到网络。登记不会直接修改系统信任。")
                .setNegativeButton("取消",null).setPositiveButton("登记此接入点",(d,w)->safe(()->{
                    TrustIdentity current=TrustWifi.read(activity);
                    if(current==null || !id.key.equals(current.key)) {toast("接入点已经变化，未登记；请重新确认");return;}
                    toast(settings.add(id.key)?"接入点已登记，请另行启用自动确认":"保存失败或已达16个上限，未确认登记");
                })).show();
    }
    private void accessibility() {
        activity.startActivity(new Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS));
    }
    private void button(LinearLayout parent,String name,Runnable click) {
        Button b=new Button(activity);b.setText(name);b.setOnClickListener(v->safe(click));parent.addView(b);
    }
    private void safe(Runnable work) { FailureBoundary.run(work,()->toast("操作未确认成功，请查看诊断")); }
    private void toast(String text) { Toast.makeText(activity,text,Toast.LENGTH_LONG).show(); }
}
