package com.lj343.jingye.connectionguard;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Resources;
import android.os.Handler;
import android.os.Looper;
import android.provider.Settings;
import android.view.accessibility.AccessibilityNodeInfo;
import java.util.ArrayList;
import java.util.List;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Official Settings only, exact labels/IDs, one click per route; no gestures/unlock. */
final class PairingUi {
    static final String OWNER="com.android.settings";
    private final AccessibilityService service;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private PairRecovery.Handle handle;
    private int checks;private boolean openedWireless,openedCode,posted;
    private final Runnable work=()->{
        posted=false;
        FailureBoundary.run(this::step,this::platformFault);
    };
    private void platformFault(){if(handle!=null)handle.fail(service,"PAIR_SETTINGS_PLATFORM_FAULT");}
    PairingUi(AccessibilityService s){service=s;}
    void start(PairRecovery.Handle h){
        handler.removeCallbacks(work);posted=false;
        handle=h;checks=0;openedWireless=false;openedCode=false;
        if(!h.ready(service))return;
        try {
            if(service.getPackageManager().checkSignatures("android",OWNER)!=PackageManager.SIGNATURE_MATCH){h.fail(service,"PAIR_SETTINGS_SIGNATURE_MISMATCH");return;}
            // Exported system developer-options route; the highlight only selects a preference.
            Intent intent=new Intent(Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS).setPackage(OWNER)
                    .putExtra(":settings:fragment_args_key","toggle_adb_wireless")
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
            service.startActivity(intent);post();
        }catch(RuntimeException error){h.fail(service,"PAIR_SETTINGS_OPEN_REJECTED");}
    }
    void event(){if(handle!=null && handle.packet==null)post();}
    void close(){handler.removeCallbacks(work);posted=false;if(handle!=null)handle.fail(service,"PAIR_ACCESSIBILITY_STOPPED");}
    private void post(){if(!posted && handle!=null && handle.packet==null){posted=true;handler.postDelayed(work,350);}}
    private void step(){
        if(handle==null || !handle.ready(service))return;
        if(++checks>60){handle.fail(service,"PAIR_SETTINGS_UI_UNSUPPORTED");return;}
        try {
            AccessibilityNodeInfo root=service.getRootInActiveWindow();
            if(root==null || !root.isVisibleToUser() || !OWNER.contentEquals(root.getPackageName()==null?"":root.getPackageName())){post();return;}
            if(service.getPackageManager().checkSignatures("android",OWNER)!=PackageManager.SIGNATURE_MATCH){handle.fail(service,"PAIR_SETTINGS_SIGNATURE_MISMATCH");return;}
            Resources resources=service.getPackageManager().getResourcesForApplication(OWNER);
            AccessibilityNodeInfo code=uniqueId(root,OWNER+":id/pairing_code");
            AccessibilityNodeInfo endpoint=uniqueId(root,OWNER+":id/ip_addr");
            if(openedCode && code!=null && endpoint!=null){
                String title=resource(resources,"adb_pairing_device_dialog_title");
                AccessibilityNodeInfo titleNode=uniqueText(root,title);
                int port=endpointPort(value(endpoint));String digits=value(code).replace(" ","");
                if(titleNode!=null && digits.matches("[0-9]{6}") && port>0 && handle.connectionPort>0){
                    handle.complete(service,port,digits);return;
                }
                post();return;
            }
            if(!openedCode){
                String pairLabel=resource(resources,"adb_pair_method_code_title");
                if(pairLabel==null)pairLabel=resource(resources,"adb_pairing_device_dialog_title");
                AccessibilityNodeInfo pair=uniqueTitle(root,pairLabel);
                if(pair==null)pair=uniqueTitle(root,"使用配对码配对设备");
                if(pair==null)pair=uniqueTitle(root,"Pair device with pairing code");
                Set<Integer> ports=new LinkedHashSet<>();collectPorts(root,ports);
                if(pair!=null && ports.size()==1){
                    handle.connectionPort=ports.iterator().next();
                    AccessibilityNodeInfo click=clickableParent(pair);
                    if(click!=null && stillSame(root) && handle.ready(service)){
                        openedCode=true;new PairSettings(service).record("PAIR_OFFICIAL_CODE_DIALOG_REQUESTED");
                        if(!click.performAction(AccessibilityNodeInfo.ACTION_CLICK)){handle.fail(service,"PAIR_CODE_DIALOG_CLICK_REJECTED");return;}
                    }
                    post();return;
                }
            }
            if(!openedWireless && !openedCode){
                String developer=resource(resources,"development_settings_title");
                boolean developerPage=uniqueText(root,developer)!=null || uniqueText(root,"开发者选项")!=null;
                String wireless=resource(resources,"enable_adb_wireless");
                AccessibilityNodeInfo row=uniqueTitle(root,wireless);
                if(row==null)row=uniqueTitle(root,"无线调试");
                if(developerPage && row!=null){
                    AccessibilityNodeInfo click=clickableParent(row);
                    if(click!=null && stillSame(root) && handle.ready(service)){
                        openedWireless=true;
                        if(!click.performAction(AccessibilityNodeInfo.ACTION_CLICK)){handle.fail(service,"PAIR_WIRELESS_DETAILS_CLICK_REJECTED");return;}
                    }
                }
            }
            post();
        }catch(Exception error){handle.fail(service,"PAIR_SETTINGS_PLATFORM_FAULT");}
    }
    private boolean stillSame(AccessibilityNodeInfo root){
        AccessibilityNodeInfo current=service.getRootInActiveWindow();
        return current!=null && current.isVisibleToUser() && current.getWindowId()==root.getWindowId()
                && OWNER.contentEquals(current.getPackageName()==null?"":current.getPackageName());
    }
    private static String resource(Resources r,String name){int id=r.getIdentifier(name,"string",OWNER);return id==0?null:r.getString(id);}
    private static String value(AccessibilityNodeInfo n){return n.getText()==null?"":n.getText().toString();}
    private static boolean plain(AccessibilityNodeInfo n){return n!=null && n.isVisibleToUser() && n.isEnabled()
            && OWNER.contentEquals(n.getPackageName()==null?"":n.getPackageName());}
    private static AccessibilityNodeInfo uniqueId(AccessibilityNodeInfo root,String id){
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByViewId(id);
        return nodes!=null && nodes.size()==1 && plain(nodes.get(0))?nodes.get(0):null;
    }
    private static AccessibilityNodeInfo uniqueTitle(AccessibilityNodeInfo root,String text){
        AccessibilityNodeInfo node=uniqueText(root,text);if(node==null)return null;
        String id=node.getViewIdResourceName();
        return "android:id/title".equals(id) || (OWNER+":id/title").equals(id)?node:null;
    }
    private static AccessibilityNodeInfo uniqueText(AccessibilityNodeInfo root,String text){
        if(text==null || text.isEmpty())return null;
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByText(text);
        AccessibilityNodeInfo result=null;
        if(nodes!=null)for(AccessibilityNodeInfo node:nodes)if(plain(node) && text.equals(value(node))){if(result!=null)return null;result=node;}
        return result;
    }
    private static AccessibilityNodeInfo clickableParent(AccessibilityNodeInfo node){
        for(int i=0;node!=null && i<5;i++,node=node.getParent())if(plain(node) && node.isClickable())return node;
        return null;
    }
    private int endpointPort(String text){
        Matcher match=Pattern.compile("^([0-9.]+):([0-9]{4,5})$").matcher(text);
        if(!match.matches() || !match.group(1).equals(handle.ip))return -1;
        int port=Integer.parseInt(match.group(2));return port>=1024 && port<=65535?port:-1;
    }
    private void collectPorts(AccessibilityNodeInfo node,Set<Integer> ports){
        if(!plain(node))return;
        int port=endpointPort(value(node));if(port>0)ports.add(port);
        for(int i=0;i<node.getChildCount();i++){AccessibilityNodeInfo child=node.getChild(i);if(child!=null)collectPorts(child,ports);}
    }
}
