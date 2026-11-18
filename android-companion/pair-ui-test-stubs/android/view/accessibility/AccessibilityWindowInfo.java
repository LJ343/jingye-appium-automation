package android.view.accessibility;
public class AccessibilityWindowInfo {
    public static final int TYPE_APPLICATION=1,TYPE_SYSTEM=3;
    public boolean active=true,focused=true;
    public int type=TYPE_APPLICATION,id=1;
    public AccessibilityNodeInfo root;
    public AccessibilityWindowInfo(AccessibilityNodeInfo root){this.root=root;}
    public boolean isActive(){return active;}
    public boolean isFocused(){return focused;}
    public int getType(){return type;}
    public int getId(){return id;}
    public AccessibilityNodeInfo getRoot(){return root;}
}
