package com.lj343.jingye.connectionguard;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import javax.crypto.Cipher;
import javax.crypto.Mac;
import javax.crypto.spec.GCMParameterSpec;
import javax.crypto.spec.SecretKeySpec;

/** Bounded, Android-independent authenticated requests and encrypted one-time codes. */
final class PairProtocol {
    static final class Request {
        final String id,nonce,fingerprint;final long expires;
        Request(String i,String n,String f,long e){id=i;nonce=n;fingerprint=f;expires=e;}
    }
    static Request request(byte[] packet,byte[] key,String id,String fingerprint,long now) {
        return request(packet,key,id,fingerprint,now,false);
    }
    static Request request(byte[] packet,byte[] key,String id,String fingerprint,long now,boolean bootstrap) {
        if(packet==null || packet.length>384 || packet.length==0 || now<=0)return null;
        for(byte b:packet)if(b<32 || b>126)return null;
        String text=new String(packet,StandardCharsets.US_ASCII);int last=text.lastIndexOf('|');
        if(last<0 || !MessageDigest.isEqual(packet,AddressProtocol.sign(text.substring(0,last),key).getBytes(StandardCharsets.US_ASCII)))return null;
        String[] p=text.substring(0,last).split("\\|",-1);
        if(p.length!=6 || !p[0].equals("JYPAIR1") || !p[1].equals("Q") || !p[2].equals(id)
                || !AddressProtocol.hex(p[3],64) || !AddressProtocol.hex(p[4],64)
                || (!p[4].equals(fingerprint) && !(bootstrap && "".equals(fingerprint))))return null;
        try {long expires=Long.parseLong(p[5]);return expires>=now && expires-now<=45?new Request(id,p[3],p[4],expires):null;}
        catch(NumberFormatException bad){return null;}
    }
    static String seal(Request r,byte[] key,String ip,int pairPort,String code,int connectionPort) throws java.security.GeneralSecurityException {
        if(r==null || !AddressProtocol.privateIpv4(ip) || pairPort<1024 || pairPort>65535
                || connectionPort<1024 || connectionPort>65535 || pairPort==connectionPort
                || code==null || !code.matches("[0-9]{6}"))throw new IllegalArgumentException("PAIR_FIELDS_INVALID");
        Mac mac=Mac.getInstance("HmacSHA256");mac.init(new SecretKeySpec(key,"HmacSHA256"));
        byte[] aesKey=mac.doFinal("jingye-pairing-aes-gcm-v1".getBytes(StandardCharsets.US_ASCII));
        byte[] iv=new byte[12];new SecureRandom().nextBytes(iv);
        Cipher cipher=Cipher.getInstance("AES/GCM/NoPadding");
        cipher.init(Cipher.ENCRYPT_MODE,new SecretKeySpec(aesKey,"AES"),new GCMParameterSpec(128,iv));
        cipher.updateAAD(("JYPAIR1|P|"+r.id+"|"+r.nonce+"|"+r.fingerprint).getBytes(StandardCharsets.US_ASCII));
        byte[] encrypted=cipher.doFinal((ip+"|"+pairPort+"|"+code+"|"+connectionPort).getBytes(StandardCharsets.US_ASCII));
        byte[] payload=new byte[iv.length+encrypted.length];System.arraycopy(iv,0,payload,0,iv.length);System.arraycopy(encrypted,0,payload,iv.length,encrypted.length);
        return AddressProtocol.sign("JYPAIR1|P|"+r.id+"|"+r.nonce+"|"+Base64.getEncoder().encodeToString(payload),key);
    }
    static String status(Request r,byte[] key,String code) {
        if(!code.matches("PAIR_[A-Z0-9_]{1,70}"))throw new IllegalArgumentException("PAIR_STATUS_INVALID");
        return AddressProtocol.sign("JYPAIR1|S|"+r.id+"|"+r.nonce+"|"+code,key);
    }
}
