package com.lj343.jingye.connectionguard;

import android.app.Activity;
import android.app.AlertDialog;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.Toast;

/** User-visible independent opt-in; installing an APK does not authorize new computers. */
final class PairSetup {
    private final Activity activity;
    PairSetup(Activity activity){this.activity=activity;}
    void addButtons(LinearLayout parent){
        button(parent,"启用本电脑自动重新配对",()->{
            PairSettings settings=new PairSettings(activity);
            if(ReporterSettings.load(activity)==null){toast("请先配置原电脑的认证地址通道；不会按IP信任陌生电脑");return;}
            new AlertDialog.Builder(activity).setTitle("允许这台电脑自动重新配对？")
                    .setMessage("仅当电脑任务确认调试认证失败，或原连接与完整发现均不可用且仍收到新鲜认证手机上报时，"
                            +"原电脑认证通道有效、网络符合您登记的AP或准确Wi-Fi名称、手机已解锁且助手运行，才会"
                            +"打开官方设置和本次配对码页面。配对码加密发送给已配置电脑，不写入日志或剪贴板。"
                            +"每十分钟最多两次请求；只在已确认的系统开发者页面最多滚动六次定位无线调试；"
                            +"界面不识别、换网络、锁屏或权限异常均停止。正常已连接时不打开配对页。"
                            +"首次启用时，允许原已配置认证通道的电脑固定自己的调试公钥；固定后拒绝其他公钥。"
                            +"本功能需要现有辅助功能同时允许读取系统设置界面，不会操作业务应用或自行解锁。")
                    .setNegativeButton("取消",null).setPositiveButton("启用",(d,w)->FailureBoundary.run(
                            ()->toast((settings.fingerprint().isEmpty()?settings.enableBootstrap():settings.enable(true))?"本电脑自动重新配对已启用":"保存失败，未确认启用"),()->toast("保存失败，未确认启用"))).show();
        });
        button(parent,"关闭本电脑自动重新配对",()->toast(new PairSettings(activity).enable(false)?"已关闭；原连接恢复和网络确认保留":"保存失败"));
    }
    private void button(LinearLayout p,String text,Runnable action){Button b=new Button(activity);b.setText(text);b.setOnClickListener(v->FailureBoundary.run(action,()->toast("操作未确认，请看诊断")));p.addView(b);}
    private void toast(String text){Toast.makeText(activity,text,Toast.LENGTH_LONG).show();}
}
