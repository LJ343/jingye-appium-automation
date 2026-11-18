package com.lj343.jingye.connectionguard;
import static com.lj343.jingye.connectionguard.TrustSettingsTest.*;

/** Exercises actual upgraded storage, matching, revocation and shared action budget. */
public final class SsidSettingsTest {
    static TrustIdentity ap(String name,int end){return TrustIdentity.of(name,String.format("12:34:56:78:9a:%02x",end));}
    public static void main(String[] args){
        count=0;
        test("upgrade never broadens legacy AP registration",()->{Ctx c=new Ctx();TrustSettings s=armed(c);check(s.ssidCount()==0&&s.match(AP).equals("EXACT_AP")&&s.match(ap(AP.ssid,2)).equals("NONE"),"old scope retained");});
        test("explicit SSID authorizes another exact-name AP",()->{TrustSettings s=new TrustSettings(new Ctx());check(s.addSsid(AP.ssid)&&s.enable(true)&&s.match(ap(AP.ssid,2)).equals("EXACT_SSID"),"name route opt in");});
        test("registration alone does not enable confirmation",()->{TrustSettings s=new TrustSettings(new Ctx());check(s.addSsid(AP.ssid)&&!s.enabled(),"no implicit enabling");});
        test("SSID matching preserves case spaces and unicode",()->{TrustSettings s=new TrustSettings(new Ctx());s.addSsid("Office É");for(String name:new String[]{"office É","Office É "," Office É","Office E\u0301","Office"})check(s.match(ap(name,2)).equals("NONE"),"exact characters");});
        test("wildcard characters are literal data",()->{TrustSettings s=new TrustSettings(new Ctx());s.addSsid("Office.*");check(s.match(ap("Office.*",2)).equals("EXACT_SSID")&&s.match(ap("OfficeEast",2)).equals("NONE"),"not a pattern");});
        test("AP and SSID hash domains remain separate",()->check(!TrustIdentity.ssidKey(AP.ssid).equals(AP.key),"separate domains"));
        test("SSID duplicate is idempotent and names stay private",()->{Ctx c=new Ctx();TrustSettings s=new TrustSettings(c);check(s.addSsid(AP.ssid)&&s.addSsid(AP.ssid)&&s.ssidCount()==1,"deduplicated");check(!c.prefs.values.toString().contains(AP.ssid),"hash only");});
        test("SSID count limit is independent from AP count",()->{Ctx c=new Ctx();TrustSettings s=armed(c);for(int i=0;i<8;i++)check(s.addSsid("Office"+i),"within limit");check(!s.addSsid("Ninth")&&s.ssidCount()==8&&s.count()==1,"independent bounded stores");});
        test("removing SSID preserves AP registration and ledger",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.addSsid(AP.ssid);s.reserve(TrustGuard.Action.ALLOW,AP,10);String ledger=c.prefs.getString("action_ledger","");check(s.removeSsid(AP.ssid)&&s.ssidCount()==0&&s.count()==1&&s.enabled(),"AP preserved");check(ledger.equals(c.prefs.getString("action_ledger","")),"ledger preserved");});
        test("removing last trust entry disables confirmation",()->{TrustSettings s=new TrustSettings(new Ctx());s.addSsid(AP.ssid);s.enable(true);check(s.removeSsid(AP.ssid)&&!s.enabled(),"no empty enabled policy");});
        test("clearing APs retains explicitly registered names",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.addSsid(AP.ssid);check(s.clear()&&s.count()==0&&s.ssidCount()==1&&!s.enabled(),"clear AP route only, disable shared feature");});
        test("SSID roaming cannot replenish action budget",()->{TrustSettings s=new TrustSettings(new Ctx());s.addSsid(AP.ssid);s.enable(true);for(int i=1;i<=3;i++)check(s.reserve(TrustGuard.Action.ALLOW,ap(AP.ssid,i),i),"one global budget");check(!s.reserve(TrustGuard.Action.ALLOW,ap(AP.ssid,4),4),"fourth AP still blocked");});
        test("AP and SSID routes share one ledger",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.addSsid(AP.ssid);s.reserve(TrustGuard.Action.ALLOW,AP,1);s.reserve(TrustGuard.Action.ALLOW,ap(AP.ssid,2),2);s.reserve(TrustGuard.Action.ALLOW,ap(AP.ssid,3),3);check(!new TrustSettings(c).reserve(TrustGuard.Action.ALLOW,ap(AP.ssid,4),4),"recreation does not reset");});
        test("revoked SSID cannot reserve another action",()->{TrustSettings s=new TrustSettings(new Ctx());s.addSsid(AP.ssid);s.enable(true);s.removeSsid(AP.ssid);check(!s.reserve(TrustGuard.Action.ALLOW,AP,1),"revocation enforced");});
        test("corrupt name store also blocks AP matching",()->{Ctx c=new Ctx();TrustSettings s=armed(c);c.prefs.values.put("ssid_allowlist","*");boolean blocked=false;try{s.match(AP);}catch(IllegalStateException good){blocked=true;}check(blocked,"no corrupt-store fallback");});
        test("invalid missing control and unknown names rejected",()->{TrustSettings s=new TrustSettings(new Ctx());for(String v:new String[]{null,"","<unknown ssid>","Office\n","x".repeat(129)})check(!s.addSsid(v),"invalid name");});
        test("redacted AP never becomes a live identity in name mode",()->{TrustSettings s=new TrustSettings(new Ctx());s.addSsid(AP.ssid);check(s.match(TrustIdentity.of(AP.ssid,"02:00:00:00:00:00")).equals("NONE"),"exact dialog identity still required");});
        test("failed persistent name write latches feature closed",()->{Ctx c=new Ctx();TrustSettings s=armed(c);c.prefs.fail=true;c.prefs.applyMemoryOnFailure=true;check(!s.addSsid(AP.ssid)&&!s.enabled()&&s.match(AP).equals("NONE"),"no action after write fault");});
        test("diagnostic discloses counts without names or hashes",()->{Ctx c=new Ctx();TrustSettings s=armed(c);s.addSsid(AP.ssid);String text=s.diagnostic();check(text.contains("allowlisted_ssid_count")&&!text.contains(AP.ssid)&&!text.contains(TrustIdentity.ssidKey(AP.ssid)),"counts only");});
        System.out.println("ALL "+count+" SSID STORAGE TESTS PASSED");
    }
}
