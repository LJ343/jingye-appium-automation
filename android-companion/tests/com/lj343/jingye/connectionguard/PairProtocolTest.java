package com.lj343.jingye.connectionguard;
import java.nio.charset.StandardCharsets;

public final class PairProtocolTest {
    static int count;
    static void check(boolean value){if(!value)throw new AssertionError();count++;}
    public static void main(String[] args)throws Exception{
        byte[] key=AddressProtocol.unhex("02".repeat(32));String id="03".repeat(16),nonce="04".repeat(32),host="05".repeat(32);
        String body="JYPAIR1|Q|"+id+"|"+nonce+"|"+host+"|1794963640";
        byte[] packet=AddressProtocol.sign(body,key).getBytes(StandardCharsets.US_ASCII);
        PairProtocol.Request r=PairProtocol.request(packet,key,id,host,1794963600);
        check(r!=null);check(PairProtocol.request(packet,key,id,"ff".repeat(32),1794963600)==null);
        check(PairProtocol.request(packet,key,id,"",1794963600)==null);
        check(PairProtocol.request(packet,key,id,"",1794963600,true)!=null);
        check(PairProtocol.request(packet,key,id,"ff".repeat(32),1794963600,true)==null);
        check(PairProtocol.request(packet,key,id,host,1794963641)==null);
        check(PairProtocol.request(packet,key,id,host,1794963500)==null);
        check(PairProtocol.request(packet,AddressProtocol.unhex("01".repeat(32)),id,host,1794963600)==null);
        packet[10]^=1;check(PairProtocol.request(packet,key,id,host,1794963600)==null);
        check(PairProtocol.request(new byte[1024],key,id,host,1794963600)==null);
        String sealed=PairProtocol.seal(r,key,"192.168.50.100",37111,"042819",37222);
        check(!sealed.contains("042819")&&!sealed.contains("192.168.50.100"));
        check(!sealed.equals(PairProtocol.seal(r,key,"192.168.50.100",37111,"042819",37222)));
        boolean denied=false;try{PairProtocol.seal(r,key,"192.168.50.100",37111,"042819",37111);}catch(IllegalArgumentException expected){denied=true;}check(denied);
        System.out.println("PASS "+count+" pairing protocol assertions");System.out.println(sealed);
    }
}
