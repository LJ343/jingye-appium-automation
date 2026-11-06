import com.lj343.jingye.connectionguard.AddressProtocol;
import java.nio.charset.StandardCharsets;

public final class AddressProtocolTest {
    public static void main(String[] args) {
        String id="01".repeat(16), nonce="03".repeat(16), challenge="04".repeat(32), ip="192.168.50.100";
        byte[] key=AddressProtocol.unhex("02".repeat(32));
        String body="JYADDR1|C|"+id+"|"+nonce+"|"+challenge;
        String signed=AddressProtocol.sign(body,key);
        check(AddressProtocol.verify(signed.getBytes(StandardCharsets.US_ASCII),key)!=null);
        String response=AddressProtocol.response(signed.getBytes(StandardCharsets.US_ASCII),key,id,nonce,ip);
        check(response!=null && response.startsWith("JYADDR1|R|"));
        check(AddressProtocol.response(signed.getBytes(StandardCharsets.US_ASCII),key,"ff".repeat(16),nonce,ip)==null);
        check(AddressProtocol.response(signed.getBytes(StandardCharsets.US_ASCII),key,id,"ff".repeat(16),ip)==null);
        check(AddressProtocol.verify(new byte[385],key)==null);
        check(AddressProtocol.verify((signed.substring(0,signed.length()-1)+"x").getBytes(StandardCharsets.US_ASCII),key)==null);
        check(AddressProtocol.privateIpv4(ip));check(AddressProtocol.privateIpv4("10.10.10.2"));
        check(!AddressProtocol.privateIpv4("127.0.0.1"));check(!AddressProtocol.privateIpv4("8.8.8.8"));
        check(!AddressProtocol.privateIpv4("192.168.020.100"));check(!AddressProtocol.privateIpv4("192.168.50.999"));
        System.out.println("PASS 12 protocol assertions");
        System.out.println(response); // Synthetic vector for the Python cross-language check.
    }
    private static void check(boolean value){if(!value)throw new AssertionError();}
}
