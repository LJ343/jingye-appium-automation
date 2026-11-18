package com.lj343.jingye.connectionguard;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.List;
public final class PairUiNodesTest {
    private static int checks;
    static void check(boolean value){checks++;if(!value)throw new AssertionError("node selection "+checks);}
    static AccessibilityNodeInfo n(String text,String id){AccessibilityNodeInfo n=new AccessibilityNodeInfo();n.text=text;n.id=id;return n;}
    public static void main(String[] args){
        AccessibilityNodeInfo root=n("","root");root.enabled=false;
        AccessibilityNodeInfo heading=n("开发人员选项","com.android.settings:id/action_bar_title");heading.enabled=false;
        root.add(heading).add(n("开发人员选项","android:id/title"));
        check(PairUiNodes.hasExactText(root,"开发人员选项"));
        check(PairUiNodes.hasPageTitle(root,"开发人员选项"));
        check(!PairUiNodes.hasPageTitle(n("开发人员选项","android:id/title"),"开发人员选项"));
        AccessibilityNodeInfo toolbar=n("","toolbar");toolbar.type="androidx.appcompat.widget.Toolbar";toolbar.add(n("开发人员选项","android:id/title"));
        check(PairUiNodes.hasPageTitle(toolbar,"开发人员选项"));
        check(!PairUiNodes.hasExactText(root,"开发人员"));
        check(!PairUiNodes.hasExactText(root,null));
        check(PairUiNodes.uniqueText(root,"开发人员选项")==null);
        AccessibilityNodeInfo action=n("无线调试","android:id/title"),row=n("","row");row.clickable=true;row.add(action);
        root.add(n("无线调试","com.android.settings:id/action_bar_title")).add(row);
        check(PairUiNodes.uniqueTitle(root,"无线调试")==action);
        check(PairUiNodes.clickableParent(action)==row);
        action.enabled=false;check(PairUiNodes.uniqueTitle(root,"无线调试")==action);
        row.enabled=false;check(PairUiNodes.clickableParent(action)==null);row.enabled=true;
        AccessibilityNodeInfo duplicate=n("无线调试","com.android.settings:id/title");root.add(duplicate);
        check(PairUiNodes.uniqueTitle(root,"无线调试")==null);
        duplicate.visible=false;check(PairUiNodes.uniqueTitle(root,"无线调试")==action);
        action.owner="other.package";check(PairUiNodes.uniqueTitle(root,"无线调试")==null);action.owner=PairUiNodes.OWNER;
        AccessibilityNodeInfo list=n("","list");list.type="androidx.recyclerview.widget.RecyclerView";list.scrollable=true;root.add(list);
        check(PairUiNodes.lists(root).size()==1);
        list.enabled=false;check(PairUiNodes.lists(root).isEmpty());list.enabled=true;
        AccessibilityNodeInfo second=n("","list2");second.type="android.widget.ListView";second.scrollable=true;root.add(second);
        check(PairUiNodes.lists(root).size()==2);second.visible=false;
        check(PairUiNodes.lists(root).size()==1);
        AccessibilityNodeInfo large=n("","large");for(int i=0;i<520;i++)large.add(n("","child"));large.add(list);
        check(PairUiNodes.lists(large).size()>1);
        AccessibilityNodeInfo unknown=n("","unknown");unknown.type="android.widget.ScrollView";unknown.scrollable=true;
        check(PairUiNodes.lists(unknown).isEmpty());
        AccessibilityNodeInfo fields=n("","fields"),code=n("synthetic","com.android.settings:id/pairing_code");fields.add(code);
        code.enabled=false;check(PairUiNodes.uniqueId(fields,code.id)==code);
        fields.add(n("synthetic",code.id));check(PairUiNodes.uniqueId(fields,code.id)==null);
        AccessibilityWindowInfo w=new AccessibilityWindowInfo(root);
        check(PairUiNodes.focused(null,List.of(w)).root==root);
        check(PairUiNodes.focused(root,List.of(w)).root==root);
        check(PairUiNodes.focused(root,List.of()).root==root);
        check(PairUiNodes.focused(root,null).root==root);
        check(PairUiNodes.focused(null,List.of(w,w)).failure.equals("PAIR_SETTINGS_FOCUS_AMBIGUOUS"));
        w.active=false;w.focused=false;check(PairUiNodes.focused(null,List.of(w)).root==null);
        w.active=true;w.type=AccessibilityWindowInfo.TYPE_SYSTEM;check(PairUiNodes.focused(null,List.of(w)).root==null);
        w.type=AccessibilityWindowInfo.TYPE_APPLICATION;w.id=2;check(PairUiNodes.focused(null,List.of(w)).root==null);
        w.id=1;root.visible=false;check(PairUiNodes.focused(null,List.of(w)).root==null);root.visible=true;
        root.owner="other.package";check(PairUiNodes.focused(root,List.of(w)).failure.equals("PAIR_SETTINGS_OWNER_MISMATCH"));root.owner=PairUiNodes.OWNER;
        AccessibilityNodeInfo foreign=n("","other");foreign.window=2;foreign.owner="other.package";
        check(PairUiNodes.focused(foreign,List.of(w)).root==null);
        check(PairUiNodes.focused(null,List.of()).failure.equals("PAIR_SETTINGS_ROOT_UNAVAILABLE"));
        System.out.println("ALL "+checks+" PAIR NODE TESTS PASSED");
    }
}
