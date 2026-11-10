package com.lj343.jingye.connectionguard;

public final class TrustWindowSessionTest {
    static int count;
    static void check(boolean b){if(!b)throw new AssertionError();}
    static void test(String name,Runnable r){r.run();count++;System.out.println("PASS "+name);}
    public static void main(String[] args){
        test("missing focused window never schedules",()->{TrustWindowSession s=new TrustWindowSession();check(!s.observe(-1,100));check(!s.take(100));});
        test("actual window keeps checkbox and allow in same burst",()->{TrustWindowSession s=new TrustWindowSession();check(s.observe(41,100));check(s.take(300));check(s.observe(41,400));check(s.take(600));});
        test("unavailable event window does not cancel followup",()->{TrustWindowSession s=new TrustWindowSession();s.observe(41,100);s.take(300);check(!s.observe(-1,350));check(s.take(500));});
        test("same focused window event storm cannot renew check budget",()->{TrustWindowSession s=new TrustWindowSession();for(int i=0;i<1000;i++){s.observe(41,100+i);if(i<6)check(s.take(100+i));else check(!s.take(100+i));}});
        test("same window cannot renew expired time",()->{TrustWindowSession s=new TrustWindowSession();s.observe(41,100);check(!s.observe(41,8101));check(!s.take(8101));});
        test("replacement window shares check budget",()->{TrustWindowSession s=new TrustWindowSession();s.observe(41,100);for(int i=0;i<5;i++)check(s.take(100));check(s.observe(42,400));check(s.take(600));check(!s.observe(43,800));});
        test("replacement window shares deadline",()->{TrustWindowSession s=new TrustWindowSession();s.observe(41,100);check(s.observe(42,8000));check(!s.take(8101));});
        test("new dialog after old burst can start",()->{TrustWindowSession s=new TrustWindowSession();s.observe(41,100);check(s.observe(42,8200));check(s.take(8400));});
        test("explicit service reset permits existing window capture",()->{TrustWindowSession s=new TrustWindowSession();s.observe(41,100);s.clear();check(s.observe(41,8200));check(s.take(8400));});
        System.out.println("ALL "+count+" WINDOW TESTS PASSED");
    }
}
