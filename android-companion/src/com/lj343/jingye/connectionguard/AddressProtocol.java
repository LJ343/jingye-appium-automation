package com.lj343.jingye.connectionguard;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/** Android-independent bounded HMAC protocol. It never changes ADB settings. */
public final class AddressProtocol {
    private AddressProtocol() {}
    public static boolean hex(String value, int length) {
        return value != null && value.length() == length && value.matches("[0-9a-f]+");
    }
    public static byte[] unhex(String value) {
        if (!hex(value, 64)) throw new IllegalArgumentException("INVALID_REPORT_KEY");
        byte[] bytes = new byte[32];
        for (int i = 0; i < bytes.length; i++) bytes[i] = (byte) Integer.parseInt(value.substring(i * 2, i * 2 + 2), 16);
        return bytes;
    }
    public static String sign(String body, byte[] key) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(key, "HmacSHA256"));
            byte[] digest = mac.doFinal(body.getBytes(StandardCharsets.US_ASCII));
            StringBuilder result = new StringBuilder(body).append('|');
            for (byte b : digest) result.append(Character.forDigit((b >>> 4) & 15, 16)).append(Character.forDigit(b & 15, 16));
            return result.toString();
        } catch (java.security.GeneralSecurityException failure) { throw new IllegalStateException("HMAC_UNAVAILABLE"); }
    }
    public static String[] verify(byte[] packet, byte[] key) {
        if (packet == null || packet.length < 1 || packet.length > 384) return null;
        for (byte b : packet) if (b < 32 || b > 126) return null;
        String text = new String(packet, StandardCharsets.US_ASCII);
        int last = text.lastIndexOf('|');
        if (last < 0 || !hex(text.substring(last + 1), 64)) return null;
        String expected = sign(text.substring(0, last), key);
        if (!MessageDigest.isEqual(expected.getBytes(StandardCharsets.US_ASCII), packet)) return null;
        String[] parts = text.substring(0, last).split("\\|", -1);
        return parts.length > 0 && "JYADDR1".equals(parts[0]) ? parts : null;
    }
    public static String response(byte[] challenge, byte[] key, String id, String phoneNonce, String ip) {
        String[] p = verify(challenge, key);
        if (p == null || p.length != 5 || !"C".equals(p[1]) || !id.equals(p[2])
                || !phoneNonce.equals(p[3]) || !hex(p[4], 64)) return null;
        return sign("JYADDR1|R|" + id + "|" + phoneNonce + "|" + p[4] + "|" + ip, key);
    }
    public static boolean privateIpv4(String value) {
        if (value == null || !value.matches("[0-9.]+")) return false;
        String[] p = value.split("\\.", -1);
        if (p.length != 4) return false;
        int[] n = new int[4];
        try {
            for (int i = 0; i < 4; i++) {
                n[i] = Integer.parseInt(p[i]);
                if (n[i] < 0 || n[i] > 255 || !Integer.toString(n[i]).equals(p[i])) return false;
            }
        } catch (NumberFormatException failure) { return false; }
        return n[0] == 10 || (n[0] == 172 && n[1] >= 16 && n[1] <= 31) || (n[0] == 192 && n[1] == 168);
    }
}
