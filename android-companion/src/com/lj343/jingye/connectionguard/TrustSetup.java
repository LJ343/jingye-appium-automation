package com.lj343.jingye.connectionguard;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.provider.Settings;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.EditText;
import android.widget.Toast;

/** Foreground-only explicit AP or exact Wi-Fi-name enrollment. No automatic imports. */
final class TrustSetup {
    static final int LOCATION_REQUEST=21;
    private final Activity activity;
    private final TrustSettings settings;
    TrustSetup(Activity a) { activity=a;settings=new TrustSettings(a); }
    void addButtons(LinearLayout layout) {
        button(layout,"登记当前接入点",this::enroll);
        button(layout,"登记指定 Wi-Fi 名称（同名全部接入点）",()->nameEntry(false));
        button(layout,"移除指定 Wi-Fi 名称",()->nameEntry(true));
        button(layout,"启用白名单自动确认",()->{
            if(settings.count()+settings.ssidCount()==0) { toast("请先登记您认可的接入点或准确 Wi-Fi 名称");return; }
            new AlertDialog.Builder(activity).setTitle("启用已授权接入点自动确认？")
                    .setMessage("仅在助手运行、手机已解锁且系统无线调试授权框匹配已登记接入点，或名称完全匹配已登记 Wi-Fi 名称时操作。"
                            +"未登记网络、无法识别的系统界面仍需您决定。每十分钟最多三次授权点击，服务重建不重置。"
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
                .setMessage("清空逐接入点名单，同时关闭自动确认；已登记 Wi-Fi 名称、系统网络信任和操作次数记录会保留。")
                .setNegativeButton("取消",null).setPositiveButton("清空",(d,w)->safe(()->toast(settings.clear()?"已清空并关闭自动确认":"保存失败，请检查诊断"))).show());
    }
    private void nameEntry(boolean remove) {
        EditText input=new EditText(activity);input.setSingleLine(true);
        TrustIdentity current=TrustWifi.read(activity);
        if(current!=null)input.setText(current.ssid);
        new AlertDialog.Builder(activity).setTitle(remove?"移除准确 Wi-Fi 名称":"登记准确 Wi-Fi 名称")
                .setMessage("请输入完整名称，大小写、空格和字符必须完全一致，不支持前缀、模糊匹配或通配符。")
                .setView(input).setNegativeButton("取消",null).setPositiveButton("下一步",(d,w)->safe(()->{
                    String name=input.getText().toString();
                    if(TrustIdentity.ssidKey(name)==null){toast("名称无效，未保存");return;}
                    new AlertDialog.Builder(activity).setTitle(remove?"移除此名称？":"信任此名称下所有接入点？")
                            .setMessage("Wi-Fi："+name+"\n\n"+(remove
                                    ?"只移除此名称；原逐接入点登记和操作预算保留。"
                                    :"您确认此准确名称下的所有接入点都可接受。与此名称相同的其他接入点也会匹配；"
                                    +"请仅登记您认可的名称。原逐接入点登记保留。仅保存名称摘要，不直接修改系统信任，也不授权其他电脑配对。"))
                            .setNegativeButton("取消",null).setPositiveButton(remove?"移除":"登记此名称",(d2,w2)->safe(()->
                                toast((remove?settings.removeSsid(name):settings.addSsid(name))
                                      ?"已保存；自动确认开关状态请查看诊断":"保存失败或名称数量已达8个上限"))).show();
                })).show();
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
