package com.lj343.jingye.connectionguard;

import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.ArrayList;
import java.util.List;
import java.util.LinkedHashSet;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

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
        return focused(active,windows,false);
    }
    static Focus focused(AccessibilityNodeInfo active,List<AccessibilityWindowInfo> windows,boolean readingRequestedDialog){
        AccessibilityNodeInfo chosen=null;
        if(windows!=null)for(AccessibilityWindowInfo window:windows){
            if(!window.isActive() && !window.isFocused())continue;
            if(window.getType()!=AccessibilityWindowInfo.TYPE_APPLICATION
                    && !(readingRequestedDialog && window.getType()==AccessibilityWindowInfo.TYPE_SYSTEM))continue;
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
    static boolean exactLabel(AccessibilityNodeInfo n,String...labels){
        if(!visible(n))return false;
        String description=n.getContentDescription()==null?"":n.getContentDescription().toString();
        for(String label:labels)if(label!=null && !label.isEmpty() && (label.equals(value(n)) || label.equals(description)))return true;
        return false;
    }
    static boolean hasLabel(AccessibilityNodeInfo root,String...labels){
        Snapshot tree=snapshot(root);if(!tree.complete)return false;
        for(AccessibilityNodeInfo n:tree.nodes)if(exactLabel(n,labels))return true;
        return false;
    }
    static final class Snapshot {
        final List<AccessibilityNodeInfo> nodes=new ArrayList<>();boolean complete=true;
    }
    static Snapshot snapshot(AccessibilityNodeInfo root){
        Snapshot tree=new Snapshot();if(!visible(root)){tree.complete=false;return tree;}
        walk(root,0,tree);return tree;
    }
    private static void walk(AccessibilityNodeInfo n,int depth,Snapshot tree){
        if(!tree.complete || !visible(n))return;
        if(depth>35 || tree.nodes.size()>=512){tree.complete=false;return;}
        tree.nodes.add(n);
        for(int i=0;i<n.getChildCount() && tree.complete;i++)walk(n.getChild(i),depth+1,tree);
    }
    static final class Action {
        final AccessibilityNodeInfo target;final String failure;
        Action(AccessibilityNodeInfo n,String code){target=n;failure=code;}
    }
    /** Only on independently verified wireless details; IDs are not OEM API contracts. */
    static Action pairingAction(AccessibilityNodeInfo root,boolean verifiedDetails,String...labels){
        if(!verifiedDetails)return new Action(null,"PAIR_DETAILS_EVIDENCE_REQUIRED");
        Snapshot tree=snapshot(root);if(!tree.complete)return new Action(null,"PAIR_ENTRY_TREE_LIMIT");
        boolean labelSeen=false;AccessibilityNodeInfo target=null;
        for(AccessibilityNodeInfo n:tree.nodes){
            if(!exactLabel(n,labels))continue;labelSeen=true;
            AccessibilityNodeInfo action=preferenceAction(n,root);
            if(action==null)continue;
            if(target!=null && !target.equals(action))return new Action(null,"PAIR_ENTRY_ACTION_AMBIGUOUS");
            target=action;
        }
        return new Action(target,target!=null?null:labelSeen?"PAIR_ENTRY_ACTION_UNAVAILABLE":"PAIR_ENTRY_LABEL_NOT_FOUND");
    }
    private static AccessibilityNodeInfo preferenceAction(AccessibilityNodeInfo n,AccessibilityNodeInfo root){
        for(int i=0;n!=null && i<5;i++,n=n.getParent()){
            if(!visible(n) || n.equals(root) || n.getWindowId()!=root.getWindowId())return null;
            String type=n.getClassName()==null?"":n.getClassName().toString();
            if(n.isCheckable() || n.isScrollable() || type.endsWith(".Switch") || type.endsWith(".CheckBox")
                    || type.endsWith(".RadioButton") || type.endsWith(".RecyclerView") || type.endsWith(".ListView")
                    || type.endsWith(".Toolbar") || type.endsWith(".EditText"))return null;
            if(plain(n) && n.isClickable())return n;
        }
        return null;
    }
    private static final Pattern ENDPOINT=Pattern.compile("^([0-9.]+):([0-9]{4,5})$");
    static int endpointPort(String text,String expectedIp){
        Matcher match=ENDPOINT.matcher(text.trim());
        if(!match.matches() || !match.group(1).equals(expectedIp))return -1;
        int port=Integer.parseInt(match.group(2));return port>=1024 && port<=65535?port:-1;
    }
    static Set<Integer> endpointPorts(AccessibilityNodeInfo root,String expectedIp,int excludePort){
        Snapshot tree=snapshot(root);if(!tree.complete)return null;
        Set<Integer> ports=new LinkedHashSet<>();
        for(AccessibilityNodeInfo n:tree.nodes){
            int port=endpointPort(value(n),expectedIp);if(port>0 && port!=excludePort)ports.add(port);
            if(n.getContentDescription()!=null){port=endpointPort(n.getContentDescription().toString(),expectedIp);if(port>0 && port!=excludePort)ports.add(port);}
        }
        return ports;
    }
    static String sixDigits(String value){
        String digits=value.replace(" ","").replace("\u00a0","").replace("\u2009","");
        return digits.matches("[0-9]{6}")?digits:null;
    }
    static final class Fields {
        final int port;final String code,failure;
        Fields(int port,String code,String failure){this.port=port;this.code=code;this.failure=failure;}
    }
    /** Read values only after the requested official dialog and exact public marker. */
    static Fields pairingFields(AccessibilityNodeInfo root,boolean dialogKnown,boolean codeMarkerKnown,String expectedIp,int connectionPort){
        if(!dialogKnown)return new Fields(0,null,"PAIR_CODE_DIALOG_TITLE_NOT_RECOGNIZED");
        if(!codeMarkerKnown)return new Fields(0,null,"PAIR_CODE_LABEL_NOT_RECOGNIZED");
        if(connectionPort<1024 || connectionPort>65535)return new Fields(0,null,"PAIR_CONNECTION_PORT_UNAVAILABLE");
        Snapshot tree=snapshot(root);if(!tree.complete)return new Fields(0,null,"PAIR_CODE_TREE_LIMIT");
        Set<String> codes=new LinkedHashSet<>();
        for(AccessibilityNodeInfo n:tree.nodes){
            String code=sixDigits(value(n));if(code!=null)codes.add(code);
            if(n.getContentDescription()!=null){code=sixDigits(n.getContentDescription().toString());if(code!=null)codes.add(code);}
        }
        if(codes.size()!=1)return new Fields(0,null,"PAIR_CODE_VALUE_NOT_UNIQUE");
        Set<Integer> ports=endpointPorts(root,expectedIp,connectionPort);
        if(ports==null || ports.size()!=1)return new Fields(0,null,"PAIR_CODE_ADDRESS_NOT_UNIQUE");
        return new Fields(ports.iterator().next(),codes.iterator().next(),null);
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
        for(int i=0;i<n.getChildCount() && out.size()<=1 && seen[0]<=512;i++)collectLists(n.getChild(i),out,depth+1,seen);
    }
}
