package com.lj343.jingye.connectionguard;

import android.accessibilityservice.AccessibilityService;
import android.app.KeyguardManager;
import android.content.pm.PackageManager;
import android.content.res.Resources;
import android.os.Handler;
import android.os.Build;
import android.os.Looper;
import android.os.PowerManager;
import android.os.SystemClock;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.List;

/** Event-driven, exact dialog adapter. Never uses coordinates, gestures, global actions or ADB. */
public final class TrustAccessibilityService extends AccessibilityService implements TrustGuard.Port {
    static volatile boolean connected;
    static volatile boolean lastRootAvailable, lastSystemWindow;
    static volatile String lastRoute="NONE";
    static volatile String lastLayout="NONE",lastWindowType="NONE";
    private static TrustAccessibilityService instance;
    private final Handler handler=new Handler(Looper.getMainLooper());
    private TrustSettings settings;
    private TrustGuard guard;
    private final TrustWindowSession session=new TrustWindowSession();
    private int officialWindow=-1;
    private String officialActivity="";
    private boolean posted, stopped, followingCheckbox;
    private AccessibilityNodeInfo checkbox, allow;
    private final Runnable work=()->{
        posted=false;
        if(!stopped && session.take(SystemClock.elapsedRealtime()))
            FailureBoundary.run(()->guard.event(),this::fault);
        if(followingCheckbox)post();
    };
    @Override protected void onServiceConnected() {
        super.onServiceConnected(); settings=new TrustSettings(this);guard=new TrustGuard(this);
        stopped=false;connected=true;instance=this;
        FailureBoundary.run(()->{settings.record("ACCESSIBILITY_CONNECTED");captureExisting();},this::fault);
    }
    @Override public void onAccessibilityEvent(AccessibilityEvent event) {
        if(stopped || event==null || settings==null || !TrustGuard.OWNER.contentEquals(event.getPackageName()==null?"":event.getPackageName()))return;
        FailureBoundary.run(()->{
            String name=event.getClassName()==null?"":event.getClassName().toString();
            if(event.getEventType()==AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED && event.getWindowId()>=0
                    && (TrustGuard.ACTIVITY.equals(name) || TrustGuard.ALIAS.equals(name))) {
                officialWindow=event.getWindowId();officialActivity=name;
            }
            // OEM events can name a removed/transient window after the checkbox click.
            // Only the actual focused root selects a target; event ids never cancel it.
            if(event.getEventType()==AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED
                    || event.getEventType()==AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED)captureExisting();
        },this::fault);
    }
    private void post() {
        if(!posted && !stopped && session.available(SystemClock.elapsedRealtime())) { posted=true;handler.postDelayed(work,200L); }
    }
    private void forget() { session.clear();officialWindow=-1;officialActivity="";followingCheckbox=false;handler.removeCallbacks(work);posted=false; }
    private void captureExisting() {
        if(stopped || !GuardService.liveArmed || !settings.enabled())return;
        Focus focused=focusedRoot();
        if(focused!=null && session.observe(focused.root.getWindowId(),SystemClock.elapsedRealtime()))post();
    }
    private static final class Focus {
        final AccessibilityNodeInfo root;final int type;
        Focus(AccessibilityNodeInfo r,int t){root=r;type=t;}
    }
    private Focus focusedRoot() {
        AccessibilityNodeInfo active=getRootInActiveWindow();
        List<AccessibilityWindowInfo> windows=getWindows();
        if(windows==null)return null;
        Focus candidate=null;
        for(AccessibilityWindowInfo w:windows) {
            if(!w.isActive() && !w.isFocused())continue;
            if(w.getType()!=AccessibilityWindowInfo.TYPE_SYSTEM
                    && !((honorDevice() || w.getId()==officialWindow) && w.getType()==AccessibilityWindowInfo.TYPE_APPLICATION))continue;
            if(active!=null && active.getWindowId()!=w.getId())continue;
            AccessibilityNodeInfo root=active!=null?active:w.getRoot();
            if(root==null || !root.isVisibleToUser() || root.getWindowId()!=w.getId()
                    || !TrustGuard.OWNER.contentEquals(root.getPackageName()==null?"":root.getPackageName()))continue;
            if(candidate!=null)return null; // Ambiguous focus never authorizes a click.
            candidate=new Focus(root,w.getType());
        }
        return candidate;
    }
    static void guardBecameReady() {
        TrustAccessibilityService service=instance;
        if(service!=null)FailureBoundary.run(service::captureExisting,service::fault);
    }
    private void fault() {
        stopped=true;forget();
        FailureBoundary.cleanup(()->settings.enable(false),()->settings.record("TRUST_PLATFORM_EXCEPTION"));
    }
    @Override public TrustGuard.Frame inspect() {
        TrustGuard.Frame f=new TrustGuard.Frame();
        f.enabled=settings.enabled();f.armed=GuardService.liveArmed;
        f.unlocked=getSystemService(PowerManager.class).isInteractive()
                && !getSystemService(KeyguardManager.class).isKeyguardLocked();
        boolean current=session.current(SystemClock.elapsedRealtime());
        f.activity=current?TrustGuard.EXISTING_SYSTEM_DIALOG:"";
        if(!f.enabled || !f.armed || !f.unlocked)return f;
        Focus focused=focusedRoot();
        AccessibilityNodeInfo root=focused==null?null:focused.root;
        f.systemWindow=focused!=null && focused.type==AccessibilityWindowInfo.TYPE_SYSTEM;
        f.applicationWindow=focused!=null && focused.type==AccessibilityWindowInfo.TYPE_APPLICATION;
        lastLayout="NONE";lastRoute="FOCUSED_SYSTEM_DIALOG_SHAPE";
        lastRootAvailable=root!=null;lastSystemWindow=f.systemWindow;
        lastWindowType=f.systemWindow?"SYSTEM":f.applicationWindow?"APPLICATION":"OTHER_OR_NOT_FOUND";
        if(root==null)return f;
        f.window=root.getWindowId();f.owner=root.getPackageName()==null?"":root.getPackageName().toString();
        if(current && f.window==officialWindow)f.activity=officialActivity;
        f.visible=root.isVisibleToUser();
        if(!TrustGuard.OWNER.equals(f.owner) || !f.visible)return f;
        f.platformSigned=getPackageManager().checkSignatures("android",TrustGuard.OWNER)==PackageManager.SIGNATURE_MATCH;
        AccessibilityNodeInfo message=unique(root,"android:id/message");
        AccessibilityNodeInfo ssidLabel=unique(root,"com.android.systemui:id/wifi_debugging_message_ssid");
        AccessibilityNodeInfo ssidValue=unique(root,"com.android.systemui:id/wifi_debugging_message_ssid_value");
        AccessibilityNodeInfo bssidLabel=unique(root,"com.android.systemui:id/wifi_debugging_message_bssid");
        AccessibilityNodeInfo bssidValue=unique(root,"com.android.systemui:id/wifi_debugging_message_bssid_value");
        AccessibilityNodeInfo honorCheck=unique(root,"com.android.systemui:id/wifi_debugging_alwaysUse");
        boolean honor=honorDevice() && absent(root,"android:id/message") && absent(root,"android:id/alwaysUse")
                && ssidLabel!=null && ssidValue!=null && bssidLabel!=null && bssidValue!=null && honorCheck!=null;
        boolean aosp=message!=null && absent(root,"com.android.systemui:id/wifi_debugging_message_ssid")
                && absent(root,"com.android.systemui:id/wifi_debugging_message_ssid_value")
                && absent(root,"com.android.systemui:id/wifi_debugging_message_bssid")
                && absent(root,"com.android.systemui:id/wifi_debugging_message_bssid_value")
                && absent(root,"com.android.systemui:id/wifi_debugging_alwaysUse");
        if(!honor && !aosp)return f;
        lastLayout=honor?"HONOR_SPLIT_V1":"AOSP_TEMPLATE";
        try {
            Resources r=getPackageManager().getResourcesForApplication(TrustGuard.OWNER);
            f.expectedTitle=text(r,"wifi_debugging_title");
            f.expectedAllow=text(r,"wifi_debugging_allow");
            f.expectedAlways=text(r,"wifi_debugging_always");
            f.expectedCancel=getResources().getString(android.R.string.cancel);
            TrustMessage.Parsed target;
            if(honor) {
                String expectedSsid=text(r,"wifi_debugging_message_ssid"),expectedBssid=text(r,"wifi_debugging_message_bssid");
                target=TrustMessage.honorSplit(expectedSsid,expectedBssid,value(ssidLabel),value(ssidValue),value(bssidLabel),value(bssidValue));
                if(target==null)return f;
                f.message=TrustMessage.splitMessage(value(ssidLabel),target.ssid,value(bssidLabel),target.bssid);
                f.expectedMessage=TrustMessage.splitMessage(expectedSsid,target.ssid,expectedBssid,target.bssid);
                f.honorSplitDialog=plain(ssidLabel) && plain(ssidValue) && plain(bssidLabel) && plain(bssidValue);
            } else {
                int messageId=r.getIdentifier("wifi_debugging_message","string",TrustGuard.OWNER);
                if(messageId==0)return f;
                f.message=value(message);
                target=TrustMessage.parse(r.getString(messageId,TrustMessage.SSID,TrustMessage.BSSID),f.message);
                if(target==null)return f;
                f.expectedMessage=r.getString(messageId,target.ssid,target.bssid);
            }
            if(target==null)return f;
            f.identity=target.identity;
            f.allowlisted=settings.contains(f.identity.key);
            if(!f.allowlisted)return f;
        } catch(PackageManager.NameNotFoundException absent) { return f; }
        // Count both supported namespaces together; ambiguous titles never fall through.
        AccessibilityNodeInfo title=uniqueAny(root,"android:id/alertTitle","com.android.systemui:id/alertTitle");
        allow=unique(root,"android:id/button1");
        AccessibilityNodeInfo cancel=unique(root,"android:id/button2");
        checkbox=honor?honorCheck:unique(root,"android:id/alwaysUse");
        if(title==null || allow==null || cancel==null || checkbox==null)return f;
        f.title=value(title);f.allow=value(allow);f.cancel=value(cancel);f.always=value(checkbox);
        f.checked=checkbox.isChecked();
        f.controlsValid=plain(title) && (honor?f.honorSplitDialog:plain(message)) && control(allow) && control(cancel)
                && control(checkbox) && checkbox.isCheckable() && !allow.isCheckable();
        return f;
    }
    private static boolean honorDevice() { return "HONOR".equalsIgnoreCase(Build.MANUFACTURER); }
    private static boolean absent(AccessibilityNodeInfo root,String id) {
        List<AccessibilityNodeInfo> values=root.findAccessibilityNodeInfosByViewId(id);
        return values!=null && values.isEmpty();
    }
    private static String text(Resources r,String name) {
        int id=r.getIdentifier(name,"string",TrustGuard.OWNER);
        return id==0?null:r.getString(id);
    }
    private static String value(AccessibilityNodeInfo n) { return n.getText()==null?null:n.getText().toString(); }
    private static boolean plain(AccessibilityNodeInfo n) { return n.isVisibleToUser() && n.isEnabled() && TrustGuard.OWNER.contentEquals(n.getPackageName()==null?"":n.getPackageName()); }
    private static boolean control(AccessibilityNodeInfo n) { return plain(n) && n.isClickable(); }
    private static AccessibilityNodeInfo unique(AccessibilityNodeInfo root,String id) {
        List<AccessibilityNodeInfo> values=root.findAccessibilityNodeInfosByViewId(id);
        return values!=null && values.size()==1?values.get(0):null;
    }
    private static AccessibilityNodeInfo uniqueAny(AccessibilityNodeInfo root,String first,String second) {
        List<AccessibilityNodeInfo> a=root.findAccessibilityNodeInfosByViewId(first),b=root.findAccessibilityNodeInfosByViewId(second);
        int sizeA=a==null?0:a.size(),sizeB=b==null?0:b.size();
        return sizeA+sizeB==1?(sizeA==1?a.get(0):b.get(0)):null;
    }
    @Override public boolean reserve(TrustGuard.Action action,String identityKey,int windowId) {
        return settings.reserve(action,identityKey,windowId);
    }
    @Override public boolean click(TrustGuard.Action action,TrustGuard.Frame expected) {
        // Re-read the system-bound AP and dialog immediately before the specific node action.
        // SystemUI tracks association changes; AdbDebuggingManager finally checks the BSSID again.
        TrustGuard.Frame fresh=inspect();
        if(TrustGuard.block(fresh)!=null || !expected.sameTarget(fresh)
                || (action==TrustGuard.Action.ALLOW)!=fresh.checked)return false;
        return (action==TrustGuard.Action.ALLOW?allow:checkbox).performAction(AccessibilityNodeInfo.ACTION_CLICK);
    }
    @Override public void record(String code) {
        settings.record(code);
        // One bounded follow-up lets the checkbox settle even if an OEM omits its content event.
        if("ALWAYS_CLICK_SENT".equals(code))followingCheckbox=true;
        if("ALLOW_CLICK_SENT_PC_UNVERIFIED".equals(code))followingCheckbox=false;
        if(followingCheckbox)post();
    }
    @Override public void onInterrupt() { forget(); }
    @Override public void onDestroy() { stopped=true;connected=false;if(instance==this)instance=null;forget();super.onDestroy(); }
}
