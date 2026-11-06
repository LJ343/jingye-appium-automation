"""Read-only wireless status using the EXISTING local ADB smart socket.

Never executes adb/the business helpers, starts a daemon, connects a transport,
scans ports, changes settings, creates Appium sessions, or writes files/logs.
Only host:devices-l, host:transport:<listed endpoint>, and the fixed read-only
shell:getprop ro.serialno request are sent. Raw identities/endpoints stay private.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
import time

CONFIG = Path(__file__).resolve().parent / "private/jingye-wireless.private.json"
ADDRESS = ("127.0.0.1", 5037)
SHANGHAI = dt.timezone(dt.timedelta(hours=8))
MESSAGES = {
    "CONNECTED": "正常：目标手机已通过无线连接实时应答，身份一致",
    "DISCONNECTED": "未连接：当前列表没有配置范围内的无线端点（可能正在恢复）",
    "OFFLINE": "连接不可用：无线端点处于 offline 状态",
    "UNAUTHORIZED": "连接不可用：手机授权尚未通过",
    "UNRESPONSIVE": "未确认：列表显示 device，但目标手机未完成实时应答",
    "IDENTITY_MISMATCH": "未确认：无线设备应答与已配置的目标手机身份不一致",
    "ADB_UNAVAILABLE": "未确认：电脑上的现有 ADB 服务无法访问",
    "CONFIG_UNAVAILABLE": "未确认：本地无线检查配置不可用",
    "UNKNOWN": "未确认：本次读取失败或状态无法识别",
}


class ProbeError(Exception):
    """Fixed code only; never include raw ADB replies or private config."""


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise TimeoutError()
    return value


def receive(stream, count, deadline):
    result = bytearray()
    while len(result) < count:
        stream.settimeout(remaining(deadline))
        part = stream.recv(count - len(result))
        if not part:
            raise ProbeError()
        result.extend(part)
    return bytes(result)


def request(stream, service, deadline):
    payload = service.encode("ascii")
    if not 1 <= len(payload) <= 65535:
        raise ProbeError()
    stream.settimeout(remaining(deadline))
    stream.sendall(f"{len(payload):04x}".encode("ascii") + payload)
    if receive(stream, 4, deadline) != b"OKAY":
        # Do not emit server-supplied failure text (may contain private values).
        raise ProbeError()


def read_length(stream, deadline):
    raw = receive(stream, 4, deadline)
    if not re.fullmatch(rb"[0-9a-fA-F]{4}", raw):
        raise ProbeError()
    return int(raw, 16)


class ExistingADB:
    def __init__(self, connect=socket.create_connection):
        self.connect = connect

    def devices(self):
        deadline = time.monotonic() + 2
        with self.connect(ADDRESS, timeout=remaining(deadline)) as stream:
            request(stream, "host:devices-l", deadline)
            length = read_length(stream, deadline)
            text = receive(stream, length, deadline).decode("utf-8", errors="strict")
        rows = []
        for line in text.splitlines():
            fields = line.split()
            if len(fields) < 2:
                raise ProbeError()
            rows.append((fields[0], fields[1]))
        return rows

    def identity(self, endpoint, deadline):
        # endpoint was selected from the CURRENT list and the configured IPv4.
        with self.connect(ADDRESS, timeout=remaining(deadline)) as stream:
            request(stream, "host:transport:" + endpoint, deadline)
            request(stream, "shell:getprop ro.serialno", deadline)
            result = bytearray()
            while True:
                stream.settimeout(remaining(deadline))
                part = stream.recv(min(513 - len(result), 256))
                if not part:
                    break
                result.extend(part)
                if len(result) > 512:
                    raise ProbeError()
        value = bytes(result).decode("utf-8", errors="strict").strip()
        if not value or any(char.isspace() for char in value):
            raise ProbeError()
        return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_config(path=CONFIG):
    # Never read .env or emit config. Optional signed cache follows a verified IP.
    with path.open("rb") as source:
        raw = source.read(65537)
    if len(raw) > 65536:
        raise ProbeError()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProbeError()
    digest = value.get("expected_identity_sha256")
    discovery = value.get("discovery")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ProbeError()
    if not isinstance(discovery, dict):
        raise ProbeError()
    addresses = discovery.get("allowed_ipv4")
    if not isinstance(addresses, list) or len(addresses) != 1:
        raise ProbeError()
    address = ipaddress.IPv4Address(addresses[0])
    if (str(address) != addresses[0] or address.is_unspecified or address.is_loopback
            or address.is_multicast or address.is_reserved or int(address) == 0xFFFFFFFF):
        raise ProbeError()
    report = value.get("address_report") or {}
    if report.get("enabled") is True:
        try:
            cache = Path(report["verified_cache_path"])
            if cache.resolve().parent != Path(path).resolve().parent or cache.stat().st_size > 2048:
                raise ValueError()
            record = json.loads(cache.read_text("ascii"))
            if set(record) != {"schema", "endpoint", "identity", "at", "mac"} or record["schema"] != 1:
                raise ValueError()
            mac = record.pop("mac")
            key_text = report["shared_key_hex"]
            if not isinstance(key_text, str) or not re.fullmatch(r"[0-9a-f]{64}", key_text):
                raise ValueError()
            expected = hmac.new(bytes.fromhex(key_text), json.dumps(record, sort_keys=True, separators=(",", ":")).encode("ascii"), hashlib.sha256).hexdigest()
            if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
                raise ValueError()
            if record["identity"] != digest or type(record["at"]) is not int or not 0 <= time.time() - record["at"] <= 86400:
                raise ValueError()
            host, raw_port = record["endpoint"].rsplit(":", 1)
            networks = [ipaddress.IPv4Network(n, strict=True) for n in report["allowed_ipv4_cidrs"]]
            if not 1 <= len(networks) <= 2 or any(not n.is_private or n.prefixlen < 24 for n in networks):
                raise ValueError()
            ip = ipaddress.IPv4Address(host)
            if not any(ip in n for n in networks) or not 1 <= int(raw_port) <= 65535:
                raise ValueError()
            return host, digest
        except (OSError, ValueError, TypeError, KeyError, UnicodeError):
            pass  # Original configured scope remains available if cache is absent.
    return addresses[0], digest


def in_scope(endpoint, allowed_ip):
    match = re.fullmatch(r"([0-9.]+):([1-9][0-9]{0,4})", endpoint)
    return bool(match and match[1] == allowed_ip and 1 <= int(match[2]) <= 65535)


def check_connection(allowed_ip, digest, client):
    try:
        rows = client.devices()
    except OSError:
        return "ADB_UNAVAILABLE", None
    except (ProbeError, ValueError, UnicodeError):
        return "UNKNOWN", None
    candidates = [(endpoint, state) for endpoint, state in rows if in_scope(endpoint, allowed_ip)]
    online = [endpoint for endpoint, state in candidates if state == "device"]
    if online:
        # Bounded even if old and new endpoints coexist during recovery.
        deadline = time.monotonic() + 6
        mismatch = False
        for endpoint in online[:4]:
            started = time.monotonic()
            try:
                actual = client.identity(endpoint, min(deadline, started + 1.5))
                if actual == digest:
                    return "CONNECTED", round((time.monotonic() - started) * 1000)
                mismatch = True
            except (OSError, ProbeError, ValueError, UnicodeError):
                pass
        return ("IDENTITY_MISMATCH" if mismatch else "UNRESPONSIVE"), None
    states = {state for _, state in candidates}
    if "unauthorized" in states:
        return "UNAUTHORIZED", None
    if "offline" in states:
        return "OFFLINE", None
    if candidates:
        return "UNKNOWN", None
    # mDNS names do not expose an IP here; do not query unrelated transports.
    if any("._adb-tls-connect._tcp" in endpoint for endpoint, _ in rows):
        return "UNKNOWN", None
    return "DISCONNECTED", None


def sample():
    started = time.monotonic()
    code, latency = "CONFIG_UNAVAILABLE", None
    try:
        allowed_ip, digest = read_config()
    except (OSError, ValueError, TypeError, ProbeError):
        pass
    else:
        code, latency = check_connection(allowed_ip, digest, ExistingADB())
    return {
        "observed_at": dt.datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "code": code,
        "message": MESSAGES[code],
        "phone_response_ms": latency,
        "check_elapsed_ms": round((time.monotonic() - started) * 1000),
    }


def main():
    parser = argparse.ArgumentParser(description="敬业无线连接只读检查，不重连、不启停服务。")
    parser.add_argument("--watch", action="store_true", help="持续查看；Ctrl+C仅退出本查看器")
    parser.add_argument("--interval", type=int, default=10, help="刷新间隔秒数，5至60，默认10")
    parser.add_argument("--json", action="store_true", help="输出不含手机标识、端点或凭据的JSON")
    args = parser.parse_args()
    if not 5 <= args.interval <= 60:
        parser.error("刷新间隔必须为5至60秒")
    if not args.json:
        print("敬业无线连接检查（只读）", flush=True)
        print("任务运行中、空闲时均可查看；本工具不会重连或启停任何服务。", flush=True)
        print("只判断检查时刻的无线链路，不代表业务进度、Appium会话或邮件结果。", flush=True)
        if args.watch:
            print(f"每轮检查后等待{args.interval}秒刷新；Ctrl+C或关闭窗口仅退出查看。", flush=True)
    try:
        while True:
            value = sample()
            if args.json:
                print(json.dumps(value, ensure_ascii=False), flush=True)
            else:
                suffix = "" if value["phone_response_ms"] is None else f"（应答{value['phone_response_ms']}毫秒）"
                print(f"[{value['observed_at']}] {value['message']}{suffix} [{value['code']}]", flush=True)
            if not args.watch:
                return 0 if value["code"] == "CONNECTED" else (1 if value["code"] in {
                    "DISCONNECTED", "OFFLINE", "UNAUTHORIZED"} else 2)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if not args.json:
            print("\n已退出查看。", flush=True)
        return 0


if __name__ == "__main__":
    sys.exit(main())
