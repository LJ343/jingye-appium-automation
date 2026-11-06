package com.lj343.jingye.connectionguard;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.SystemClock;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.nio.charset.StandardCharsets;
import java.security.SecureRandom;

/** One bounded exchange every 15 seconds while armed and the switch is on.
 * No multicast, internet service, subnet scan, setting writes or ADB operations.
 * A packet sent is not proof of PC ADB connectivity.
 */
final class AddressReporter {
    private final Context context;
    private HandlerThread thread;
    private Handler handler;
    private volatile boolean enabled, closed;
    private volatile DatagramSocket active;
    private long nextSend;
    AddressReporter(Context context) { this.context = context.getApplicationContext(); }
    synchronized void setEnabled(boolean value) {
        if (closed) return;
        enabled = value;
        if (!value) {
            if (handler != null) handler.removeCallbacks(work);
            DatagramSocket socket = active; if (socket != null) socket.close();
            return;
        }
        if (thread == null) {
            thread = new HandlerThread("address-report"); thread.start(); handler = new Handler(thread.getLooper());
        }
        handler.removeCallbacks(work); handler.post(work);
    }
    private final Runnable work = new Runnable() {
        @Override public void run() {
            if (closed || !enabled) return;
            long now = SystemClock.elapsedRealtime();
            if (now >= nextSend) {
                nextSend = now + 15_000;
                try { exchange(); } catch (Exception failure) { /* Bounded, no raw errors or secret logs. */ }
            }
            synchronized (AddressReporter.this) {
                if (!closed && enabled) { handler.removeCallbacks(this); handler.postDelayed(this, Math.max(1000, nextSend - SystemClock.elapsedRealtime())); }
            }
        }
    };
    private void exchange() throws Exception {
        ReporterSettings config = ReporterSettings.load(context);
        if (config == null || closed || !enabled) return;
        ConnectivityManager manager = context.getSystemService(ConnectivityManager.class);
        Network selected = null; String ip = null;
        for (Network network : manager.getAllNetworks()) {
            NetworkCapabilities caps = manager.getNetworkCapabilities(network);
            if (caps == null || !caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)) continue;
            LinkProperties props = manager.getLinkProperties(network);
            if (props == null) continue;
            for (LinkAddress address : props.getLinkAddresses()) {
                String value = address.getAddress().getHostAddress();
                if (!(address.getAddress() instanceof Inet4Address) || !AddressProtocol.privateIpv4(value)) continue;
                if (ip != null && !ip.equals(value)) return; // Ambiguous Wi-Fi: do not guess.
                selected = network; ip = value;
            }
        }
        if (selected == null || ip == null || closed || !enabled) return;
        byte[] nonceBytes = new byte[16]; new SecureRandom().nextBytes(nonceBytes);
        StringBuilder nonce = new StringBuilder();
        for (byte b : nonceBytes) nonce.append(Character.forDigit((b >>> 4) & 15, 16)).append(Character.forDigit(b & 15, 16));
        try (DatagramSocket socket = new DatagramSocket(null)) {
            active = socket;
            selected.bindSocket(socket);
            socket.connect(InetAddress.getByName(config.host), config.port);
            socket.setSoTimeout(2000);
            if (closed || !enabled) return;
            byte[] hello = AddressProtocol.sign("JYADDR1|A|" + config.id + "|" + nonce + "|" + ip, config.key).getBytes(StandardCharsets.US_ASCII);
            socket.send(new DatagramPacket(hello, hello.length));
            byte[] buffer = new byte[385]; DatagramPacket packet = new DatagramPacket(buffer, buffer.length);
            socket.receive(packet);
            byte[] challenge = java.util.Arrays.copyOf(packet.getData(), packet.getLength());
            String response = AddressProtocol.response(challenge, config.key, config.id, nonce.toString(), ip);
            if (response == null || closed || !enabled) return;
            byte[] answer = response.getBytes(StandardCharsets.US_ASCII);
            socket.send(new DatagramPacket(answer, answer.length));
        } finally { active = null; }
    }
    synchronized void close() {
        closed = true; enabled = false;
        DatagramSocket socket = active; if (socket != null) socket.close();
        if (handler != null) handler.removeCallbacksAndMessages(null);
        if (thread != null) thread.quitSafely();
    }
}
