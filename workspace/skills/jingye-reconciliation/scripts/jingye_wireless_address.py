"""Authenticated phone-IP rendezvous, active only inside connection recovery.

No ADB, port scanning, settings or business operations. A signed fresh challenge
response permits ONE source IPv4; the caller must still verify the phone by ADB.
Wire messages contain only addresses, random nonces and a provisioned random id.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import threading
import time

MAGIC = "JYADDR1"
HEX32 = re.compile(r"[0-9a-f]{32}")
HEX64 = re.compile(r"[0-9a-f]{64}")
MAX_PACKET = 384


def sign(body, key):
    return body + "|" + hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()


def decode(packet, key):
    if not isinstance(packet, bytes) or not 1 <= len(packet) <= MAX_PACKET:
        return None
    try:
        text = packet.decode("ascii")
        body, mac = text.rsplit("|", 1)
        if not HEX64.fullmatch(mac) or not hmac.compare_digest(sign(body, key), text):
            return None
        parts = body.split("|")
        return parts if parts[0] == MAGIC else None
    except (ValueError, UnicodeError):
        return None


def validate_config(config):
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise ValueError("address_report_disabled_or_invalid")
    if set(config) != {"enabled", "listen_ipv4", "listen_port", "reporter_id",
                       "shared_key_hex", "allowed_ipv4_cidrs", "verified_cache_path"}:
        raise ValueError("address_report_config_keys_invalid")
    host = ipaddress.IPv4Address(config["listen_ipv4"])
    if not host.is_private or host.is_unspecified or host.is_multicast or host.is_loopback:
        raise ValueError("address_report_listen_address_invalid")
    port = config["listen_port"]
    if type(port) is not int or not 1024 <= port <= 65535:
        raise ValueError("address_report_port_invalid")
    if not isinstance(config["reporter_id"], str) or not HEX32.fullmatch(config["reporter_id"]):
        raise ValueError("address_report_id_invalid")
    if not isinstance(config["shared_key_hex"], str) or not HEX64.fullmatch(config["shared_key_hex"]):
        raise ValueError("address_report_key_invalid")
    raw = config["allowed_ipv4_cidrs"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= 2:
        raise ValueError("address_report_network_invalid")
    networks = tuple(ipaddress.IPv4Network(value, strict=True) for value in raw)
    if any(not n.is_private or n.prefixlen < 24 or n.is_loopback or n.is_link_local for n in networks):
        raise ValueError("address_report_network_too_broad")
    cache = Path(config["verified_cache_path"])
    if not cache.is_absolute() or cache.suffix != ".json":
        raise ValueError("address_report_cache_invalid")
    return bytes.fromhex(config["shared_key_hex"]), networks


class Handshake:
    """Clock and nonce injectable for tests; all untrusted input is bounded."""
    def __init__(self, config, *, clock=time.monotonic, nonce=lambda: secrets.token_hex(32)):
        self.key, self.networks = validate_config(config)
        self.reporter = config["reporter_id"]
        self.clock, self.nonce = clock, nonce
        self.pending = {}
        self.accepted = None
        self.lock = threading.Lock()

    def in_scope(self, value):
        try:
            ip = ipaddress.IPv4Address(value)
            return str(ip) == value and any(ip in n and ip not in (n.network_address, n.broadcast_address)
                                           for n in self.networks)
        except (ValueError, TypeError):
            return False

    def handle(self, packet, source):
        parts = decode(packet, self.key)
        if parts is None or len(parts) not in (5, 6):
            return None
        if parts[2] != self.reporter or not HEX32.fullmatch(parts[3]) or not self.in_scope(source[0]):
            return None
        now = self.clock()
        self.pending = {k: v for k, v in self.pending.items() if now - v[1] <= 5}
        peer = (source[0], source[1], parts[3])
        if parts[1] == "A" and len(parts) == 5 and parts[4] == source[0]:
            if len(self.pending) >= 8:
                return None
            challenge = self.nonce()
            self.pending[peer] = (challenge, now)
            return sign("|".join((MAGIC, "C", self.reporter, parts[3], challenge)), self.key).encode("ascii")
        if parts[1] == "R" and len(parts) == 6 and parts[5] == source[0] and HEX64.fullmatch(parts[4]):
            expected = self.pending.pop(peer, None)
            if expected is not None and hmac.compare_digest(expected[0], parts[4]):
                with self.lock:
                    self.accepted = (source[0], now)
        return None

    def latest_ip(self):
        with self.lock:
            value = self.accepted
        return value[0] if value is not None and self.clock() - value[1] <= 45 else None


class AddressReceiver:
    def __init__(self, config):
        self.config = config
        self.protocol = Handshake(config)
        self.sock = None
        self.thread = None
        self.closed = threading.Event()

    def start(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # No REUSEADDR: another receiver owns this port, so fail closed.
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            sock.bind((self.config["listen_ipv4"], self.config["listen_port"]))
            sock.settimeout(0.2)
        except BaseException:
            sock.close()
            raise
        self.sock = sock
        self.thread = threading.Thread(target=self._serve, name="jingye-address-rendezvous", daemon=True)
        try:
            self.thread.start()
        except BaseException:
            sock.close()
            self.thread = None
            raise
        return self

    def _serve(self):
        while not self.closed.is_set():
            try:
                packet, source = self.sock.recvfrom(MAX_PACKET + 1)
                reply = self.protocol.handle(packet, source)
                if reply is not None:
                    self.sock.sendto(reply, source)
            except socket.timeout:
                continue
            except OSError:
                break

    def latest_ip(self):
        return self.protocol.latest_ip()

    def close(self):
        self.closed.set()
        if self.sock is not None:
            self.sock.close()
        if self.thread is not None:
            self.thread.join(timeout=0.5)


def save_verified_endpoint(config, endpoint, expected_identity):
    """Call ONLY after an actual matching ro.serialno read, never on IP receipt."""
    key, networks = validate_config(config)
    host, port = endpoint.rsplit(":", 1)
    ip = ipaddress.IPv4Address(host)
    if not any(ip in n for n in networks) or not 1 <= int(port) <= 65535 or not HEX64.fullmatch(expected_identity):
        raise ValueError("verified_endpoint_invalid")
    value = {"schema": 1, "endpoint": endpoint, "identity": expected_identity, "at": int(time.time())}
    body = json.dumps(value, sort_keys=True, separators=(",", ":"))
    value["mac"] = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
    path = Path(config["verified_cache_path"])
    temporary = path.with_name(path.name + "." + secrets.token_hex(8) + ".tmp")
    try:
        with temporary.open("x", encoding="ascii") as stream:
            stream.write(json.dumps(value, separators=(",", ":")))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def read_verified_endpoint(config, expected_identity):
    key, networks = validate_config(config)
    try:
        path = Path(config["verified_cache_path"])
        if path.stat().st_size > 2048:
            return None
        value = json.loads(path.read_text("ascii"))
        if set(value) != {"schema", "endpoint", "identity", "at", "mac"} or value["schema"] != 1:
            return None
        mac = value.pop("mac")
        body = json.dumps(value, sort_keys=True, separators=(",", ":"))
        expected = hmac.new(key, body.encode("ascii"), hashlib.sha256).hexdigest()
        if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
            return None
        if value["identity"] != expected_identity or type(value["at"]) is not int or not 0 <= time.time() - value["at"] <= 86400:
            return None
        host, port = value["endpoint"].rsplit(":", 1)
        ip = ipaddress.IPv4Address(host)
        return value["endpoint"] if any(ip in n for n in networks) and 1 <= int(port) <= 65535 else None
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        return None
