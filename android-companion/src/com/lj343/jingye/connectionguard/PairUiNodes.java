package com.lj343.jingye.connectionguard;

import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.ArrayList;
import java.util.List;

/** Node matching for official Settings only. Page evidence is not an action target. */
final class PairUiNodes {
    static final String OWNER="com.android.settings";
    static String value(AccessibilityNodeInfo n){return n.getText()==null?"":n.getText().toString();}
    static boolean visible(AccessibilityNodeInfo n){return n!=null && n.isVisibleToUser()
            && OWNER.contentEquals(n.getPackageName()==null?"":n.getPackageName());}
    static boolean plain(AccessibilityNodeInfo n){return visible(n) && n.isEnabled();}
    static final class Focus {
        final AccessibilityNodeInfo root;final String failure;
        Focus(AccessibilityNodeInfo root,String failure){this.root=root;this.failure=failure;}
    }
    static Focus focused(AccessibilityNodeInfo active,List<AccessibilityWindowInfo> windows){
        AccessibilityNodeInfo chosen=null;
        if(windows!=null)for(AccessibilityWindowInfo window:windows){
            if(!window.isActive() && !window.isFocused())continue;
            if(window.getType()!=AccessibilityWindowInfo.TYPE_APPLICATION)continue;
            if(active!=null && active.getWindowId()!=window.getId())continue;
            AccessibilityNodeInfo root=active!=null?active:window.getRoot();
            if(!visible(root) || root.getWindowId()!=window.getId())continue;
            if(chosen!=null)return new Focus(null,"PAIR_SETTINGS_FOCUS_AMBIGUOUS");
            chosen=root;
        }
        if(chosen!=null)return new Focus(chosen,null);
        if((windows==null || windows.isEmpty()) && visible(active))return new Focus(active,null);
        return new Focus(null,active==null?"PAIR_SETTINGS_ROOT_UNAVAILABLE":
                !OWNER.contentEquals(active.getPackageName()==null?"":active.getPackageName())?
                "PAIR_SETTINGS_OWNER_MISMATCH":"PAIR_SETTINGS_FOCUSED_ROOT_UNAVAILABLE");
    }
    static AccessibilityNodeInfo uniqueId(AccessibilityNodeInfo root,String id){
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByViewId(id);
        return nodes!=null && nodes.size()==1 && visible(nodes.get(0))?nodes.get(0):null;
    }
    static boolean hasExactText(AccessibilityNodeInfo root,String text){
        if(text==null || text.isEmpty())return false;
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByText(text);
        if(nodes!=null)for(AccessibilityNodeInfo n:nodes)if(visible(n) && text.equals(value(n)))return true;
        return false;
    }
    static boolean hasPageTitle(AccessibilityNodeInfo root,String text){
        if(text==null || text.isEmpty())return false;
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByText(text);
        if(nodes!=null)for(AccessibilityNodeInfo n:nodes){
            if(!visible(n) || !text.equals(value(n)))continue;
            String id=n.getViewIdResourceName();
            if("android:id/action_bar_title".equals(id) || (OWNER+":id/action_bar_title").equals(id)
                    || (OWNER+":id/toolbar_title").equals(id) || (OWNER+":id/collapsing_toolbar_title").equals(id))return true;
            AccessibilityNodeInfo parent=n.getParent();
            for(int i=0;parent!=null && i<5 && visible(parent);i++,parent=parent.getParent()){
                String type=parent.getClassName()==null?"":parent.getClassName().toString();
                if(type.equals("android.widget.Toolbar") || type.endsWith(".Toolbar") || type.endsWith(".CollapsingToolbarLayout"))return true;
            }
        }
        return false;
    }
    static AccessibilityNodeInfo uniqueText(AccessibilityNodeInfo root,String text){
        if(text==null || text.isEmpty())return null;
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByText(text);
        AccessibilityNodeInfo result=null;
        if(nodes!=null)for(AccessibilityNodeInfo node:nodes)if(visible(node) && text.equals(value(node))){
            if(result!=null)return null;result=node;
        }
        return result;
    }
    static AccessibilityNodeInfo uniqueTitle(AccessibilityNodeInfo root,String text){
        if(text==null || text.isEmpty())return null;
        List<AccessibilityNodeInfo> nodes=root.findAccessibilityNodeInfosByText(text);
        AccessibilityNodeInfo result=null;
        if(nodes!=null)for(AccessibilityNodeInfo node:nodes){
            if(!visible(node) || !text.equals(value(node)))continue;
            String id=node.getViewIdResourceName();
            if(!"android:id/title".equals(id) && !(OWNER+":id/title").equals(id))continue;
            if(result!=null)return null;result=node;
        }
        return result;
    }
    static AccessibilityNodeInfo clickableParent(AccessibilityNodeInfo node){
        for(int i=0;node!=null && i<5;i++,node=node.getParent()){
            if(!visible(node))return null;
            if(plain(node) && node.isClickable())return node;
        }
        return null;
    }
    static List<AccessibilityNodeInfo> lists(AccessibilityNodeInfo root){
        List<AccessibilityNodeInfo> out=new ArrayList<>();int[] seen={0};collectLists(root,out,0,seen);
        if(seen[0]>512 && out.size()<2){out.add(null);out.add(null);}return out;
    }
    private static void collectLists(AccessibilityNodeInfo n,List<AccessibilityNodeInfo> out,int depth,int[] seen){
        if(n==null || out.size()>1 || !visible(n))return;
        if(depth>35 || ++seen[0]>512){seen[0]=513;return;}
        String type=n.getClassName()==null?"":n.getClassName().toString();
        if(plain(n) && n.isScrollable() && (type.equals("android.widget.ListView") || type.endsWith(".RecyclerView")))out.add(n);
        for(int i=0;i<n.getChildCount() && out.size()<=1;i++)collectLists(n.getChild(i),out,depth+1,seen);
    }
}
