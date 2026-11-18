package android.view.accessibility;
import java.util.ArrayList;
import java.util.List;
/** In-memory accessibility tree; test-only, never packaged in the APK. */
public class AccessibilityNodeInfo {
    public String text="",owner="com.android.settings",id="",type="android.widget.TextView";
    public boolean visible=true,enabled=true,clickable,scrollable;
    public int window=1;
    private AccessibilityNodeInfo parent;
    private final List<AccessibilityNodeInfo> children=new ArrayList<>();
    public AccessibilityNodeInfo add(AccessibilityNodeInfo n){children.add(n);n.parent=this;return this;}
    public CharSequence getText(){return text;}
    public CharSequence getPackageName(){return owner;}
    public String getViewIdResourceName(){return id;}
    public CharSequence getClassName(){return type;}
    public boolean isVisibleToUser(){return visible;}
    public boolean isEnabled(){return enabled;}
    public boolean isClickable(){return clickable;}
    public boolean isScrollable(){return scrollable;}
    public int getWindowId(){return window;}
    public AccessibilityNodeInfo getParent(){return parent;}
    public int getChildCount(){return children.size();}
    public AccessibilityNodeInfo getChild(int i){return children.get(i);}
    public List<AccessibilityNodeInfo> findAccessibilityNodeInfosByText(String s){
        List<AccessibilityNodeInfo> out=new ArrayList<>();if(text!=null && text.contains(s))out.add(this);
        for(AccessibilityNodeInfo n:children)out.addAll(n.findAccessibilityNodeInfosByText(s));return out;
    }
    public List<AccessibilityNodeInfo> findAccessibilityNodeInfosByViewId(String s){
        List<AccessibilityNodeInfo> out=new ArrayList<>();if(s.equals(id))out.add(this);
        for(AccessibilityNodeInfo n:children)out.addAll(n.findAccessibilityNodeInfosByViewId(s));return out;
    }
}
