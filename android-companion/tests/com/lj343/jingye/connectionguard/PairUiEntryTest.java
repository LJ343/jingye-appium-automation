package com.lj343.jingye.connectionguard;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.util.List;

public final class PairUiEntryTest {
    private static int checks;
    private static final String LABEL="使用配对码配对设备",IP="192.168.50.100";
    static void check(boolean value){checks++;if(!value)throw new AssertionError("entry selection "+checks);}
    static AccessibilityNodeInfo n(String text){AccessibilityNodeInfo n=new AccessibilityNodeInfo();n.text=text;return n;}
    static AccessibilityNodeInfo row(){AccessibilityNodeInfo n=n("");n.type="android.widget.LinearLayout";n.clickable=true;return n;}
    static AccessibilityNodeInfo rootWith(AccessibilityNodeInfo child){return n("").add(child);}
    static PairUiNodes.Fields fields(AccessibilityNodeInfo root){return PairUiNodes.pairingFields(root,true,true,IP,37222);}
    public static void main(String[] args){
        AccessibilityNodeInfo label=n(LABEL);label.id="com.android.settings:id/honor_preference_label";
        AccessibilityNodeInfo row=row().add(label),root=rootWith(row);
        check(PairUiNodes.uniqueTitle(root,LABEL)==null);
        check(PairUiNodes.pairingAction(root,true,LABEL).target==row);
        check(PairUiNodes.pairingAction(root,false,LABEL).failure.equals("PAIR_DETAILS_EVIDENCE_REQUIRED"));
        row.add(n(LABEL));check(PairUiNodes.pairingAction(root,true,LABEL).target==row);
        AccessibilityNodeInfo other=row().add(n(LABEL));root.add(other);
        check(PairUiNodes.pairingAction(root,true,LABEL).failure.equals("PAIR_ENTRY_ACTION_AMBIGUOUS"));
        other.visible=false;check(PairUiNodes.pairingAction(root,true,LABEL).target==row);
        other.visible=true;other.owner="foreign.owner";check(PairUiNodes.pairingAction(root,true,LABEL).target==row);
        row.enabled=false;check(PairUiNodes.pairingAction(root,true,LABEL).failure.equals("PAIR_ENTRY_ACTION_UNAVAILABLE"));row.enabled=true;
        AccessibilityNodeInfo partial=row().add(n("请"+LABEL));check(PairUiNodes.pairingAction(rootWith(partial),true,LABEL).target==null);
        AccessibilityNodeInfo described=row();described.description=LABEL;check(PairUiNodes.pairingAction(rootWith(described),true,LABEL).target==described);
        described.checkable=true;check(PairUiNodes.pairingAction(rootWith(described),true,LABEL).target==null);described.checkable=false;
        described.type="android.widget.Switch";check(PairUiNodes.pairingAction(rootWith(described),true,LABEL).target==null);
        described.type="android.widget.EditText";check(PairUiNodes.pairingAction(rootWith(described),true,LABEL).target==null);
        described.type="android.widget.LinearLayout";described.scrollable=true;check(PairUiNodes.pairingAction(rootWith(described),true,LABEL).target==null);
        AccessibilityNodeInfo global=n(LABEL);global.clickable=true;check(PairUiNodes.pairingAction(global,true,LABEL).target==null);
        AccessibilityNodeInfo toolbar=row().add(n(LABEL));toolbar.type="android.widget.Toolbar";check(PairUiNodes.pairingAction(rootWith(toolbar),true,LABEL).target==null);
        AccessibilityNodeInfo wrongWindow=row().add(n(LABEL));wrongWindow.window=2;check(PairUiNodes.pairingAction(rootWith(wrongWindow),true,LABEL).target==null);
        AccessibilityNodeInfo large=n("");large.add(row().add(n(LABEL)));for(int i=0;i<520;i++)large.add(n(""));
        check(!PairUiNodes.snapshot(large).complete);
        check(PairUiNodes.pairingAction(large,true,LABEL).failure.equals("PAIR_ENTRY_TREE_LIMIT"));
        AccessibilityNodeInfo deep=n(""),at=deep;for(int i=0;i<40;i++){AccessibilityNodeInfo next=n("");at.add(next);at=next;}
        check(!PairUiNodes.snapshot(deep).complete);
        check(PairUiNodes.hasLabel(root,LABEL));
        check(!PairUiNodes.hasLabel(root,"配对码"));
        check(PairUiNodes.hasLabel(rootWith(described),LABEL));
        AccessibilityWindowInfo system=new AccessibilityWindowInfo(root);system.type=AccessibilityWindowInfo.TYPE_SYSTEM;
        check(PairUiNodes.focused(null,List.of(system)).root==null);
        check(PairUiNodes.focused(null,List.of(system),true).root==root);
        check(PairUiNodes.focused(null,List.of(system,system),true).root==null);

        AccessibilityNodeInfo dialog=n("").add(n("与设备配对")).add(n("WLAN配对码")).add(n("042819")).add(n(IP+":37111"));
        check(fields(dialog).failure==null);
        check(fields(dialog).port==37111 && fields(dialog).code.equals("042819"));
        check(PairUiNodes.pairingFields(dialog,false,true,IP,37222).failure.equals("PAIR_CODE_DIALOG_TITLE_NOT_RECOGNIZED"));
        check(PairUiNodes.pairingFields(dialog,true,false,IP,37222).failure.equals("PAIR_CODE_LABEL_NOT_RECOGNIZED"));
        check(PairUiNodes.pairingFields(dialog,true,true,IP,0).failure.equals("PAIR_CONNECTION_PORT_UNAVAILABLE"));
        check(PairUiNodes.pairingFields(dialog,true,true,"192.168.50.101",37222).failure.equals("PAIR_CODE_ADDRESS_NOT_UNIQUE"));
        dialog.add(n(IP+":37222"));check(fields(dialog).failure==null);
        dialog.add(n("042819"));check(fields(dialog).failure==null);
        dialog.add(n(IP+":37112"));check(fields(dialog).failure.equals("PAIR_CODE_ADDRESS_NOT_UNIQUE"));
        AccessibilityNodeInfo two=n("").add(n("042819")).add(n("999999")).add(n(IP+":37111"));check(fields(two).failure.equals("PAIR_CODE_VALUE_NOT_UNIQUE"));
        AccessibilityNodeInfo grouped=n("").add(n("042\u2009819")).add(n(IP+":37111"));check(fields(grouped).code.equals("042819"));
        AccessibilityNodeInfo desc=n("");desc.description="042819";
        check(fields(n("").add(desc).add(n(IP+":37111"))).failure==null);
        AccessibilityNodeInfo foreignCode=n("042819");foreignCode.owner="foreign.owner";
        check(fields(n("").add(foreignCode).add(n(IP+":37111"))).failure.equals("PAIR_CODE_VALUE_NOT_UNIQUE"));
        check(fields(n("").add(n("042819")).add(n(IP+":37222"))).failure.equals("PAIR_CODE_ADDRESS_NOT_UNIQUE"));
        check(fields(large).failure.equals("PAIR_CODE_TREE_LIMIT"));
        check(PairUiNodes.endpointPorts(large,IP,-1)==null);
        check(PairUiNodes.endpointPort(" "+IP+":37111 ",IP)==37111);
        check(PairUiNodes.endpointPort(IP+":65536",IP)==-1);
        check(PairUiNodes.endpointPort(IP+":999",IP)==-1);
        check(PairUiNodes.endpointPort("192.168.50.101:37111",IP)==-1);
        check(PairUiNodes.sixDigits("042819").equals("042819"));
        check(PairUiNodes.sixDigits("0428190")==null);
        check(PairUiNodes.sixDigits("042-819")==null);
        System.out.println("ALL "+checks+" PAIR ENTRY TESTS PASSED");
    }
}
