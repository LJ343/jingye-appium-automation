package com.lj343.jingye.connectionguard;

import java.util.function.Consumer;

public final class TrustGuardTest {
    static int count;
    static final String AP="12:34:56:78:9a:bc";
    static final TrustIdentity ID=TrustIdentity.of("测试网络",AP);
    static void check(boolean ok,String why){if(!ok)throw new AssertionError(why);}
    static void test(String name,Runnable work){work.run();count++;System.out.println("PASS "+name);}
    static TrustGuard.Frame frame() {
        TrustGuard.Frame f=new TrustGuard.Frame();
        f.enabled=f.armed=f.unlocked=f.platformSigned=f.visible=f.allowlisted=f.controlsValid=true;
        f.owner=TrustGuard.OWNER;f.activity=TrustGuard.ACTIVITY;f.window=41;f.identity=ID;
        f.title=f.expectedTitle="允许无线调试？";
        f.message=f.expectedMessage="测试网络\n"+AP;
        f.allow=f.expectedAllow="允许";f.always=f.expectedAlways="始终允许";f.cancel=f.expectedCancel="取消";
        return f;
    }
    static final class Rig implements TrustGuard.Port {
        TrustGuard.Frame current=frame();String ledger="",code="";int clicks,reads;boolean failCommit,rejectClick;
        Consumer<Rig> change;
        public TrustGuard.Frame inspect(){reads++;if(reads==2 && change!=null)change.accept(this);return current;}
        public boolean reserve(TrustGuard.Action a,String key,int window){
            String next=TrustLedger.reserve(ledger,a,key,window,1_000_000L);
            if(next==null || failCommit)return false;ledger=next;return true;
        }
        public boolean click(TrustGuard.Action a,TrustGuard.Frame expected){
            check(!ledger.isEmpty(),"budget persisted before action");
            if(rejectClick)return false;clicks++;
            if(a==TrustGuard.Action.CHECK_ALWAYS)current.checked=true;return true;
        }
        public void record(String value){code=value;}
        void event(){new TrustGuard(this).event();reads=0;}
    }
    static void blocked(String name,Consumer<TrustGuard.Frame> mutate){test(name,()->{Rig r=new Rig();mutate.accept(r.current);r.event();check(r.clicks==0 && r.ledger.isEmpty(),"no UI action or reservation");});}
    public static void main(String[] args){
        test("two stage checkbox then allow only after reinspection",()->{Rig r=new Rig();r.event();check(r.clicks==1 && r.code.equals("ALWAYS_CLICK_SENT"),"checkbox only");r.event();check(r.clicks==2 && r.code.equals("ALLOW_CLICK_SENT_PC_UNVERIFIED"),"then allow");});
        test("already checked requires one allow action",()->{Rig r=new Rig();r.current.checked=true;r.event();check(r.clicks==1 && r.ledger.contains("ALLOW"),"one explicit approval");});
        test("official system dialog alias keeps exact content checks",()->{Rig r=new Rig();r.current.activity=TrustGuard.ALIAS;r.event();check(r.clicks==1,"supported alias");});
        test("service rebind can recognize an existing focused system dialog",()->{Rig r=new Rig();r.current.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;r.current.systemWindow=true;r.event();check(r.clicks==1,"full system dialog shape");});
        blocked("shape fallback refuses untyped windows",f->f.activity=TrustGuard.EXISTING_SYSTEM_DIALOG);
        blocked("application window without exact Honor shape refused",f->{f.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;f.applicationWindow=true;});
        test("verified Honor split dialog supports its application window",()->{Rig r=new Rig();r.current.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;r.current.applicationWindow=true;r.current.honorSplitDialog=true;r.event();check(r.clicks==1,"OEM type still needs full shape and whitelist");});
        blocked("Honor marker alone cannot authorize an untyped window",f->{f.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;f.honorSplitDialog=true;});
        blocked("Honor shape still refuses unknown AP",f->{f.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;f.applicationWindow=true;f.honorSplitDialog=true;f.allowlisted=false;});
        blocked("Honor shape still refuses substituted owner",f->{f.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;f.applicationWindow=true;f.honorSplitDialog=true;f.owner="example.fake";});
        blocked("shape fallback cannot approve a similar system message",f->{f.activity=TrustGuard.EXISTING_SYSTEM_DIALOG;f.systemWindow=true;f.message="其他网络";});
        test("event storm never repeats allow on same dialog",()->{Rig r=new Rig();r.current.checked=true;for(int i=0;i<1000;i++)r.event();check(r.clicks==1,"duplicates rejected");});
        test("service recreation retains spent window action",()->{Rig r=new Rig();r.current.checked=true;r.event();Rig next=new Rig();next.current.checked=true;next.ledger=r.ledger;next.event();check(next.clicks==0,"no new budget");});
        blocked("default disabled",f->f.enabled=false);
        blocked("guard paused or not recreated",f->f.armed=false);
        blocked("locked or screen off",f->f.unlocked=false);
        blocked("unsigned or substituted owner",f->f.platformSigned=false);
        blocked("other application impersonating title",f->f.owner="example.fake");
        blocked("ADB pairing dialog not network dialog",f->f.activity="com.android.systemui.usb.UsbDebuggingActivity");
        blocked("unknown AP requires human",f->f.allowlisted=false);
        blocked("unavailable AP identity",f->f.identity=null);
        blocked("not active or visible",f->f.visible=false);
        blocked("unrecognized or duplicate controls",f->f.controlsValid=false);
        blocked("unknown window",f->f.window=-1);
        blocked("wrong title",f->f.title="允许连接？");
        blocked("changed network in message",f->f.message="其他网络");
        blocked("wrong affirmative button",f->f.allow="连接");
        blocked("wrong persistent checkbox",f->f.always="记住电脑");
        blocked("missing negative control",f->f.cancel=null);
        blocked("missing localized reference strings",f->f.expectedTitle=null);
        test("AP changes between reservation and action",()->{Rig r=new Rig();r.change=x->{x.current=frame();x.current.identity=TrustIdentity.of("测试网络","12:34:56:78:9a:bd");};r.event();check(r.clicks==0 && !r.ledger.isEmpty(),"spent but no wrong-target action");});
        test("dialog replaced between reservation and action",()->{Rig r=new Rig();r.change=x->{x.current=frame();x.current.window=42;};r.event();check(r.clicks==0,"replacement refused");});
        test("layout replaced between reservation and action",()->{Rig r=new Rig();r.change=x->{x.current=frame();x.current.honorSplitDialog=true;};r.event();check(r.clicks==0,"layout change refused");});
        test("user pauses between reservation and action",()->{Rig r=new Rig();r.change=x->{x.current=frame();x.current.armed=false;};r.event();check(r.clicks==0,"pause wins");});
        test("failed storage commits cannot click",()->{Rig r=new Rig();r.failCommit=true;r.event();check(r.clicks==0,"write ahead failure stops");});
        test("system rejects action without implicit retry",()->{Rig r=new Rig();r.rejectClick=true;r.event();check(r.code.equals("SYSTEM_CLICK_REJECTED"),"reported");r.event();check(r.clicks==0,"not retried");});
        test("three approvals per ten minutes survive restart",()->{Rig r=new Rig();r.current.checked=true;for(int i=0;i<4;i++){r.current.window=100+i;r.event();}check(r.clicks==3,"fourth denied");});
        test("checkbox and allow each have bounded budget",()->{String raw="";for(int i=0;i<3;i++){raw=TrustLedger.reserve(raw,TrustGuard.Action.CHECK_ALWAYS,ID.key,i,1_000_000);check(raw!=null,"checkbox allowance");raw=TrustLedger.reserve(raw,TrustGuard.Action.ALLOW,ID.key,i,1_000_000);check(raw!=null,"approval allowance");}check(TrustLedger.reserve(raw,TrustGuard.Action.ALLOW,ID.key,8,1_000_000)==null,"limit");});
        test("expired records permit another real episode",()->{String raw=TrustLedger.reserve("",TrustGuard.Action.ALLOW,ID.key,1,1_000_000);check(TrustLedger.reserve(raw,TrustGuard.Action.ALLOW,ID.key,1,1_600_000)!=null,"ten minute expiry");});
        test("clock rollback refuses future ledger records",()->{String raw=TrustLedger.reserve("",TrustGuard.Action.ALLOW,ID.key,1,1_000_000);check(TrustLedger.reserve(raw,TrustGuard.Action.ALLOW,ID.key,2,999_999)==null,"fail closed");});
        test("corrupt or unbounded ledger fails closed",()->{for(String bad:new String[]{"junk","-1,ALLOW,"+ID.key+",1","1,INVALID,"+ID.key+",1","x".repeat(4097)})check(TrustLedger.reserve(bad,TrustGuard.Action.ALLOW,ID.key,2,1_000_000)==null,"invalid record");});
        test("SSID alone does not authorize another AP",()->check(!ID.key.equals(TrustIdentity.of(ID.ssid,"12:34:56:78:9a:bd").key),"BSSID matters"));
        test("same BSSID with different SSID remains distinct",()->check(!ID.key.equals(TrustIdentity.of("其他网络",AP).key),"SSID matters"));
        test("canonical BSSID does not alter AP identity",()->check(ID.key.equals(TrustIdentity.of(ID.ssid,AP.toUpperCase()).key),"case normalization"));
        test("redacted multicast missing and control identities denied",()->{for(String b:new String[]{"02:00:00:00:00:00","00:00:00:00:00:00","ff:ff:ff:ff:ff:ff","12:34:56:78:9a",""})check(TrustIdentity.of("Lab",b)==null,"invalid BSSID");check(TrustIdentity.of("<unknown ssid>",AP)==null,"unknown");check(TrustIdentity.of("Lab\n",AP)==null,"control");});
        test("localized exact template with SSID first",()->{String t="网络 "+TrustMessage.SSID+"\n接入点 "+TrustMessage.BSSID;TrustMessage.Parsed p=TrustMessage.parse(t,"网络 测试网络\n接入点 "+AP);check(p!=null && p.identity.key.equals(ID.key),"exact fields");});
        test("localized reversed fields and uppercase BSSID",()->{String t=TrustMessage.BSSID+" / "+TrustMessage.SSID;check(TrustMessage.parse(t,AP.toUpperCase()+" / 测试网络").identity.key.equals(ID.key),"argument order");});
        test("template mismatch or extra content rejected",()->{String t=TrustMessage.SSID+" / "+TrustMessage.BSSID;check(TrustMessage.parse(t,"测试网络 | "+AP)==null,"layout");check(TrustMessage.parse(t,"测试网络 / "+AP+" 允许") ==null,"extra");});
        test("template without two unique AP fields is unsupported",()->{for(String t:new String[]{"",TrustMessage.SSID,TrustMessage.BSSID,TrustMessage.SSID+TrustMessage.SSID+TrustMessage.BSSID})check(TrustMessage.parse(t,"x")==null,"do not broaden OEM support");});
        test("regex characters in SSID are data",()->{String t="["+TrustMessage.SSID+"] ("+TrustMessage.BSSID+")";check(TrustMessage.parse(t,"[Lab.*(x)] ("+AP+")").ssid.equals("Lab.*(x)"),"literal SSID");});
        test("Honor separated labels bind both exact AP values",()->{TrustMessage.Parsed p=TrustMessage.honorSplit("网络","接入点","网络",ID.ssid,"接入点",AP);check(p!=null && p.identity.key.equals(ID.key),"same whitelist identity");});
        test("Honor labels are exact not fuzzy",()->check(TrustMessage.honorSplit("网络","接入点","网络 ",ID.ssid,"接入点",AP)==null,"extra label text refused"));
        test("Honor missing resource labels are unsupported",()->check(TrustMessage.honorSplit(null,"接入点","网络",ID.ssid,"接入点",AP)==null,"no guessed label"));
        test("Honor redacted or malformed AP rejected",()->check(TrustMessage.honorSplit("网络","接入点","网络",ID.ssid,"接入点","02:00:00:00:00:00")==null,"no redacted AP"));
        test("Honor same SSID at different BSSID stays distinct",()->{TrustMessage.Parsed p=TrustMessage.honorSplit("网络","接入点","网络",ID.ssid,"接入点","12:34:56:78:9a:bd");check(p!=null && !p.identity.key.equals(ID.key),"no SSID-only enrollment");});
        test("Honor structural serialization is unambiguous",()->check(!TrustMessage.splitMessage("a","b:c","d",AP).equals(TrustMessage.splitMessage("a:b","c","d",AP)),"field boundaries remain exact"));
        System.out.println("ALL "+count+" TRUST TESTS PASSED");
    }
}
