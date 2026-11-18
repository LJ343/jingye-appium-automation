package com.lj343.jingye.connectionguard;
public final class PairUiPolicyTest {
    private static int checks;
    static void check(boolean value){checks++;if(!value)throw new AssertionError("scroll policy "+checks);}
    public static void main(String[] args){
        PairUiPolicy p=new PairUiPolicy();
        check(!p.reserveScroll(false,true,false,1,0));
        check(!p.reserveScroll(true,false,false,1,0));
        check(!p.reserveScroll(true,true,true,1,0));
        check(!p.reserveScroll(true,true,false,0,0));
        check(!p.reserveScroll(true,true,false,2,0));
        check(!p.reserveScroll(true,true,false,1,-1));
        check(p.used()==0);
        check(p.reserveScroll(true,true,false,1,0));
        check(!p.reserveScroll(true,true,false,1,699));
        check(p.reserveScroll(true,true,false,1,700));
        for(int i=2;i<6;i++)check(p.reserveScroll(true,true,false,1,i*700));
        check(!p.reserveScroll(true,true,false,1,100000));
        check(p.used()==6);
        System.out.println("ALL "+checks+" PAIR UI TESTS PASSED");
    }
}
