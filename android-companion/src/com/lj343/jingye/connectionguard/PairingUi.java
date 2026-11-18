package com.lj343.jingye.connectionguard;

import android.accessibilityservice.AccessibilityService;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.content.res.Resources;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.os.Bundle;
import android.provider.Settings;
import android.view.accessibility.AccessibilityNodeInfo;
import java.util.List;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import static com.lj343.jingye.connectionguard.PairUiNodes.*;

/** Official Settings only, exact labels/IDs, one click per route; no gestures/unlock. */
final class PairingUi {
    static final String OWNER="com.android.settings";
    private final AccessibilityService service;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private PairRecovery.Handle handle;
    private int checks;private boolean openedWireless,openedCode,posted;
    private PairUiPolicy navigation;
    private final Runnable work=()->{
        posted=false;
        FailureBoundary.run(this::step,this::platformFault);
    };
    private void platformFault(){if(handle!=null)handle.fail(service,"PAIR_SETTINGS_PLATFORM_FAULT");}
    PairingUi(AccessibilityService s){service=s;}
    void start(PairRecovery.Handle h){
        handler.removeCallbacks(work);posted=false;
        handle=h;checks=0;openedWireless=false;openedCode=false;navigation=new PairUiPolicy();stage("PAIR_WAIT_SETTINGS_WINDOW");
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
        if(++checks>80){handle.timeout(service);return;}
        try {
            AccessibilityNodeInfo root=officialRoot();
            if(root==null){post();return;}
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
                stage("PAIR_CODE_FIELDS_NOT_MATCHED");post();return;
            }
            if(!openedCode){
                String pairLabel=resource(resources,"adb_pair_method_code_title");
                if(pairLabel==null)pairLabel=resource(resources,"adb_pairing_device_dialog_title");
                AccessibilityNodeInfo pair=uniqueTitle(root,pairLabel);
                if(pair==null)pair=uniqueTitle(root,"使用配对码配对设备");
                if(pair==null)pair=uniqueTitle(root,"Pair device with pairing code");
                Set<Integer> ports=new LinkedHashSet<>();collectPorts(root,ports);
                if(pair!=null && ports.size()==1){
                    handle.connectionPort=ports.iterator().next();stage("PAIR_CODE_DIALOG_NOT_VISIBLE");
                    AccessibilityNodeInfo click=clickableParent(pair);
                    if(click!=null && stillSame(root) && handle.ready(service)){
                        openedCode=true;new PairSettings(service).record("PAIR_OFFICIAL_CODE_DIALOG_REQUESTED");
                        if(!click.performAction(AccessibilityNodeInfo.ACTION_CLICK)){handle.fail(service,"PAIR_CODE_DIALOG_CLICK_REJECTED");return;}
                    }
                    post();return;
                }
                if(pair!=null){stage("PAIR_CONNECTION_ADDRESS_NOT_UNIQUE");post();return;}
            }
            if(!openedWireless && !openedCode){
                String developer=resource(resources,"development_settings_title");
                boolean developerPage=hasPageTitle(root,developer) || hasPageTitle(root,"开发者选项")
                        || hasPageTitle(root,"开发人员选项");
                String wireless=resource(resources,"enable_adb_wireless");
                AccessibilityNodeInfo row=uniqueTitle(root,wireless);
                if(row==null)row=uniqueTitle(root,"无线调试");
                if(developerPage && row!=null){
                    AccessibilityNodeInfo click=clickableParent(row);
                    if(click!=null && stillSame(root) && handle.ready(service)){
                        openedWireless=true;
                        stage("PAIR_WIRELESS_DETAILS_NOT_VISIBLE");
                        if(!click.performAction(AccessibilityNodeInfo.ACTION_CLICK)){handle.fail(service,"PAIR_WIRELESS_DETAILS_CLICK_REJECTED");return;}
                    }else stage("PAIR_WIRELESS_TARGET_UNAVAILABLE");
                }else if(developerPage){
                    stage("PAIR_WIRELESS_ROW_NOT_FOUND");
                    List<AccessibilityNodeInfo> lists=lists(root);
                    if(lists.isEmpty())stage("PAIR_DEVELOPER_LIST_NOT_FOUND");
                    else if(lists.size()>1)stage("PAIR_DEVELOPER_LIST_AMBIGUOUS");
                    else if(navigation.used()>=6)stage("PAIR_DEVELOPER_SCROLL_BUDGET_EXHAUSTED");
                    if(navigation.reserveScroll(true,true,false,lists.size(),SystemClock.elapsedRealtime())){
                        AccessibilityNodeInfo list=lists.get(0);
                        if(stillSame(root) && handle.ready(service)){
                            new PairSettings(service).record("PAIR_DEVELOPER_LIST_SCROLL_RESERVED");
                            boolean top=false;
                            if(navigation.used()==1)for(AccessibilityNodeInfo.AccessibilityAction action:list.getActionList())
                                if(action.getId()==AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION.getId())top=true;
                            Bundle position=new Bundle();position.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_ROW_INT,0);
                            position.putInt(AccessibilityNodeInfo.ACTION_ARGUMENT_COLUMN_INT,0);
                            boolean moved=top?list.performAction(AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION.getId(),position)
                                    :list.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD);
                            if(!moved){
                                handle.fail(service,"PAIR_DEVELOPER_SCROLL_REJECTED");return;
                            }
                        }
                    }
                }else stage("PAIR_DEVELOPER_TITLE_NOT_FOUND");
            }
            post();
        }catch(Exception error){handle.fail(service,"PAIR_SETTINGS_PLATFORM_FAULT");}
    }
    private void stage(String code){if(handle!=null)handle.timeoutCode=code;}
    private AccessibilityNodeInfo officialRoot(){
        PairUiNodes.Focus focus=PairUiNodes.focused(service.getRootInActiveWindow(),service.getWindows());
        if(focus.root==null)stage(focus.failure);
        return focus.root;
    }
    private boolean stillSame(AccessibilityNodeInfo root){
        AccessibilityNodeInfo current=officialRoot();
        return current!=null && current.isVisibleToUser() && current.getWindowId()==root.getWindowId()
                && OWNER.contentEquals(current.getPackageName()==null?"":current.getPackageName());
    }
    private static String resource(Resources r,String name){int id=r.getIdentifier(name,"string",OWNER);return id==0?null:r.getString(id);}
    private int endpointPort(String text){
        Matcher match=Pattern.compile("^([0-9.]+):([0-9]{4,5})$").matcher(text);
        if(!match.matches() || !match.group(1).equals(handle.ip))return -1;
        int port=Integer.parseInt(match.group(2));return port>=1024 && port<=65535?port:-1;
    }
    private void collectPorts(AccessibilityNodeInfo node,Set<Integer> ports){
        collectPorts(node,ports,0,new int[]{0});
    }
    private void collectPorts(AccessibilityNodeInfo node,Set<Integer> ports,int depth,int[] seen){
        if(!visible(node) || depth>35 || ++seen[0]>512)return;
        int port=endpointPort(value(node));if(port>0)ports.add(port);
        for(int i=0;i<node.getChildCount();i++)collectPorts(node.getChild(i),ports,depth+1,seen);
    }
}
