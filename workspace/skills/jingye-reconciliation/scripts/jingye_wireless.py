"""Opt-in, identity-checked wireless ADB transport; importing performs no I/O.

Only JINGYE_WIRELESS_ENABLED=1 enables the module. JINGYE_WIRELESS_CONFIG
points to a non-secret JSON config. No service restart, pairing, app actions,
session creation, or business replay is performed here.
The fixed binary and daemon version are checked before CLI calls. An external
replacement between a check and execution remains a TOCTOU boundary.
"""
from __future__ import annotations

import hashlib
import contextlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import threading
import time


PINNED_ADB_PATH = r"C:\Users\YOUR_USER\adb\platform-tools\adb.exe"
PINNED_ADB_SHA256 = "957e46b8615f7af5b7292a2ddabe98d2e61940c3fb2b0545756507f080613e71"


class ConnectionUnavailable(RuntimeError):
    # Discovery must propagate control failures, not treat them as bad candidates.
    wireless_discovery_fatal = True

    def __init__(self, code="wireless_connection_unavailable"):
        self.code = code
        super().__init__(code)


class _ADBServerUnavailable(OSError):
    pass


def _binary_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _server_version(timeout):
    """Read the existing smart socket without the CLI's self-management path."""
    deadline = time.monotonic() + min(timeout, 2)
    with socket.create_connection(("127.0.0.1", 5037), timeout=min(timeout, 2)) as stream:
        def receive(count):
            value = b""
            while len(value) < count:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise OSError("adb_server_probe_timeout")
                stream.settimeout(remaining)
                part = stream.recv(count - len(value))
                if not part:
                    raise OSError("adb_server_probe_closed")
                value += part
            return value
        stream.sendall(b"000chost:version")
        if receive(4) != b"OKAY":
            raise OSError("adb_server_probe_rejected")
        try:
            length = int(receive(4), 16)
            if not 1 <= length <= 16:
                raise ValueError()
            return int(receive(length), 16)
        except ValueError:
            raise OSError("adb_server_probe_invalid") from None


def is_enabled():
    return os.environ.get("JINGYE_WIRELESS_ENABLED") == "1"


def _text(value):
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")


def _endpoint(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9.]+:[0-9]{1,5}", value):
        return None
    host, port = value.rsplit(":", 1)
    try:
        address = ipaddress.IPv4Address(host)
        if str(address) != host or address.is_unspecified or address.is_multicast or address.is_loopback:
            return None
        return value if 1 <= int(port) <= 65535 and str(int(port)) == port else None
    except ValueError:
        return None


def _wireless_serial(value):
    return bool(_endpoint(value) or (isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9_-]{1,160}\._adb-tls-connect\._tcp\.?", value)))


def _safe_retry(arguments):
    if arguments[:1] == ["pull"]:
        return (len(arguments) == 3 and arguments[1].startswith("/")
                and not arguments[2].startswith("-") and arguments[2].endswith(".part"))
    if arguments == ["get-state"]:
        return True
    if arguments[:1] != ["shell"]:
        return False
    tokens = arguments[1:]
    if len(tokens) == 1:
        # Simple old shell strings are supported; compound shell programs never replay.
        if re.search(r"[;&|<>`$\n\r]", tokens[0]):
            return False
        try:
            tokens = shlex.split(tokens[0])
        except ValueError:
            return False
    if tokens == ["getprop", "ro.serialno"]:
        return True
    if not tokens or tokens[0] not in {"ls", "stat", "wc", "sha256sum"}:
        return False
    # Read-only utilities, no shell operators/substitution; explicit callers opt in.
    return all(not re.search(r"[;&|<>`$\n\r]", token) for token in tokens)


class WirelessADB:
    def __init__(self, config, *, runner=None, clock=None, sleeper=None,
                 server_version_probe=None, binary_digest_reader=None):
        self.config = dict(config)
        self.adb_path = str(Path(config.get("adb_path", "")))
        identity_hash = config.get("expected_identity_sha256", "")
        if not isinstance(identity_hash, str):
            raise ConnectionUnavailable("wireless_config_invalid")
        self.identity_hash = identity_hash.lower()
        self.cached_endpoint = config.get("cached_endpoint") or None
        self.interval = float(config.get("wait_interval_seconds", 5))
        self.max_wait = float(config.get("max_wait_seconds", 300))
        self.heartbeat = float(config.get("heartbeat_seconds", 60))
        self.command_timeout = float(config.get("command_timeout_seconds", 10))
        self.log_path = Path(config.get("log_path", ""))
        if (not Path(self.adb_path).is_absolute() or not self.log_path.is_absolute()
                or not re.fullmatch(r"[0-9a-f]{64}", self.identity_hash)
                or (self.cached_endpoint and not _endpoint(self.cached_endpoint))
                or not 0.05 <= self.interval <= 300 or not 0 <= self.max_wait <= 86400
                or not 0.05 <= self.heartbeat <= 300 or not 0.1 <= self.command_timeout <= 60):
            raise ConnectionUnavailable("wireless_config_invalid")
        self.runner = runner or subprocess.run
        self.binary_digest_reader = binary_digest_reader or _binary_digest
        self.server_version_probe = server_version_probe or _server_version
        self.clock = clock or time.monotonic
        self.sleep = sleeper or time.sleep
        self.lock = threading.RLock()
        self.serial = None
        self.discovery_reason = None
        self._address_receiver = None
        self._address_receiver_attempted = False
        self._reported_ip = None
        self._saved_endpoint = None
        self._discovery_addresses = None
        self._last_heartbeat = float("-inf")
        self._verify_binary()

    def _verify_binary(self):
        # Deployment pin: this exact platform-tools 37.0.0 executable was checked
        # read-only. A config cannot substitute an older self-managing adb client.
        if os.path.normcase(os.path.abspath(self.adb_path)) != os.path.normcase(PINNED_ADB_PATH):
            raise ConnectionUnavailable("wireless_adb_path_not_pinned")
        try:
            digest = self.binary_digest_reader(self.adb_path)
        except OSError:
            raise ConnectionUnavailable("wireless_adb_binary_unreadable") from None
        if digest != PINNED_ADB_SHA256:
            raise ConnectionUnavailable("wireless_adb_binary_hash_mismatch")

    @contextlib.contextmanager
    def _operation(self):
        deadline = None if self.max_wait == 0 else self.clock() + self.max_wait
        acquired = self.lock.acquire() if deadline is None else self.lock.acquire(timeout=self.max_wait)
        if not acquired:
            raise ConnectionUnavailable("wireless_wait_budget_exhausted")
        try:
            yield deadline
        finally:
            receiver = self._address_receiver
            self._address_receiver = None
            self._address_receiver_attempted = False
            self._reported_ip = None
            try:
                if receiver is not None:
                    receiver.close()
            finally:
                self.lock.release()

    def _log(self, event, **fields):
        # No device ro.serialno, raw stdout/stderr, UI, filenames or credentials.
        value = {"atUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 "event": event, **fields}
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False) + "\n")
                stream.flush()
        except OSError:
            raise ConnectionUnavailable("wireless_log_unavailable") from None

    def _exec(self, args, timeout, *, capture_output=True, text=True, deadline=None):
        # One command budget includes binary verification, version probe and CLI.
        # A positive API max_wait is a shared deadline, never reset by recovery.
        call_deadline = self.clock() + timeout
        if deadline is not None:
            call_deadline = min(call_deadline, deadline)
        self._verify_binary()
        def remaining():
            left = call_deadline - self.clock()
            if left <= 0:
                if deadline is not None and self.clock() >= deadline:
                    raise ConnectionUnavailable("wireless_wait_budget_exhausted")
                raise subprocess.TimeoutExpired("pinned_adb_operation", timeout)
            return left
        # Fixed platform-tools 37.0.0 uses ADB protocol 1.0.41. Reject a mismatched
        # daemon before the CLI can enter its version-mismatch kill/relaunch path.
        try:
            version = self.server_version_probe(remaining())
        except OSError:
            raise _ADBServerUnavailable("wireless_adb_server_unavailable") from None
        if version != 0x29:
            raise ConnectionUnavailable("wireless_adb_server_version_mismatch")
        # A literal remote host also prevents autostart if the daemon disappears
        # after this read. Neither this module nor the probe starts a server.
        options = {"encoding": "utf-8", "errors": "replace"} if text else {}
        return self.runner([self.adb_path, "-H", "127.0.0.1", "-P", "5037", *args],
            timeout=remaining(), check=False, capture_output=capture_output, text=text,
            stdin=subprocess.DEVNULL, **options)

    def _budget(self, deadline):
        if deadline is None:
            return self.command_timeout
        left = deadline - self.clock()
        if left <= 0:
            raise ConnectionUnavailable("wireless_wait_budget_exhausted")
        return min(self.command_timeout, left)

    def _verify(self, serial, *, connect=False, remaining_s=None, operation_deadline=None):
        if not _wireless_serial(serial):
            return False
        verification_deadline = None if remaining_s is None else self.clock() + remaining_s
        def verification_timeout():
            timeout = self._budget(operation_deadline)
            if verification_deadline is not None:
                remaining = verification_deadline - self.clock()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("wireless_identity_verification", remaining_s)
                timeout = min(timeout, remaining)
            return timeout
        try:
            if connect:
                result = self._exec(["connect", serial], verification_timeout(), deadline=operation_deadline)
                if result.returncode:
                    return False
            result = self._exec(["-s", serial, "get-state"], verification_timeout(), deadline=operation_deadline)
            if result.returncode or _text(result.stdout).strip() != "device":
                return False
            result = self._exec(["-s", serial, "shell", "getprop", "ro.serialno"], verification_timeout(), deadline=operation_deadline)
            identity = _text(result.stdout).strip()
            if result.returncode or not identity:
                return False
            if hashlib.sha256(identity.encode("utf-8")).hexdigest() != self.identity_hash:
                self._log("candidate_identity_rejected")
                return False
            self.serial = serial
            if _endpoint(serial):
                # Cache only an authenticated endpoint, in memory; never rewrite config.
                self.cached_endpoint = serial
                if self._reported_ip == serial.rsplit(":", 1)[0] and self._saved_endpoint != serial:
                    try:
                        from jingye_wireless_address import save_verified_endpoint
                        save_verified_endpoint(self.config["address_report"], serial, self.identity_hash)
                        self._saved_endpoint = serial
                    except (OSError, ValueError, TypeError):
                        self._log("verified_endpoint_cache_unavailable")
            return True
        except (subprocess.TimeoutExpired, OSError, ConnectionUnavailable) as error:
            if isinstance(error, ConnectionUnavailable):
                raise
            return False

    def _candidates(self, deadline):
        seen = set()
        try:
            result = self._exec(["devices"], self._budget(deadline), deadline=deadline)
            if result.returncode == 0:
                for line in _text(result.stdout).splitlines():
                    fields = line.split()
                    if len(fields) >= 2 and fields[1] == "device" and _wireless_serial(fields[0]):
                        seen.add(fields[0])
                        yield fields[0], False
            result = self._exec(["mdns", "services"], self._budget(deadline), deadline=deadline)
            if result.returncode == 0:
                for line in _text(result.stdout).splitlines():
                    fields = line.split()
                    if len(fields) >= 3 and fields[-2].rstrip(".") == "_adb-tls-connect._tcp" and _endpoint(fields[-1]):
                        if fields[-1] not in seen:
                            seen.add(fields[-1])
                            yield fields[-1], True
        except (subprocess.TimeoutExpired, OSError):
            pass
        if self.cached_endpoint and self.cached_endpoint not in seen:
            yield self.cached_endpoint, True

    def _ensure(self, deadline):
        while True:
            self._budget(deadline)
            for serial, connect in self._candidates(deadline):
                previous_serial = self.serial
                if self._verify(serial, connect=connect, operation_deadline=deadline):
                    if serial != previous_serial:
                        self._log("connection_verified")
                    return serial
            report_config = self.config.get("address_report") or {}
            if report_config.get("enabled") is True and not self._address_receiver_attempted:
                self._address_receiver_attempted = True
                try:
                    from jingye_wireless_address import AddressReceiver
                    self._address_receiver = AddressReceiver(report_config).start()
                    self._log("address_receiver_waiting")
                except (OSError, ValueError, TypeError, ImportError):
                    self._log("address_receiver_unavailable")
            reported_ip = self._address_receiver.latest_ip() if self._address_receiver is not None else None
            discovery_config = dict(self.config.get("discovery") or {})
            if reported_ip is not None:
                # Signed fresh rendezvous changes ONE target, never scans its subnet.
                if reported_ip != self._reported_ip:
                    self._log("authenticated_phone_address_received")
                self._reported_ip = reported_ip
                discovery_config["allowed_ipv4"] = [reported_ip]
            elif self._reported_ip is not None:
                # Do not continue probing a stale address after a report expires.
                self._reported_ip = None
            if discovery_config.get("enabled") is True:
                try:
                    from jingye_wireless_discovery import EndpointDiscovery
                except ImportError:
                    raise ConnectionUnavailable("wireless_discovery_module_missing") from None
                addresses = tuple(discovery_config.get("allowed_ipv4", ()))
                if not hasattr(self, "_discovery") or self._discovery_addresses != addresses:
                    try:
                        self._discovery = EndpointDiscovery(discovery_config)
                        self._discovery_addresses = addresses
                    except (TypeError, ValueError):
                        raise ConnectionUnavailable("wireless_discovery_config_invalid") from None
                serial = self._discovery.discover(
                    max_seconds=None if deadline is None else max(0, deadline - self.clock()),
                    verify_candidate=lambda endpoint, remaining_s:
                    self._verify(endpoint, connect=True, remaining_s=remaining_s, operation_deadline=deadline))
                report = self._discovery.last_report
                if report.get("reason") != "cooldown" or self.discovery_reason != "cooldown":
                    self._log("endpoint_discovery", **report)
                self.discovery_reason = report.get("reason")
                if serial and self.serial == serial:
                    self._log("connection_verified_by_discovery")
                    return serial
            now = self.clock()
            if now - self._last_heartbeat >= self.heartbeat:
                self._log("waiting_for_connection", maxWaitSeconds=self.max_wait,
                          discoveryReason=self.discovery_reason)
                self._last_heartbeat = now
            self.sleep(self.interval if deadline is None else min(self.interval, max(0, deadline - self.clock())))

    def ensure_connected(self):
        with self._operation() as deadline:
            try:
                return self._ensure(deadline)
            except ConnectionUnavailable as error:
                self._log("connection_unavailable", code=error.code)
                raise

    def _arguments(self, args):
        if isinstance(args, (str, bytes)):
            raise ValueError("wireless_adb_requires_argument_array")
        args = [os.fspath(value) for value in args]
        if not args or any(not isinstance(value, str) or "\x00" in value for value in args):
            raise ValueError("wireless_adb_invalid_arguments")
        if args[0].lower() in {"adb", "adb.exe", self.adb_path.lower()}:
            args = args[1:]
        if args[:1] == ["-s"] and len(args) >= 3:
            args = args[2:]
        if not args or args[0].startswith("-") or args[0] in {
                "kill-server", "start-server", "reboot", "root", "unroot", "tcpip", "usb",
                "pair", "connect", "disconnect", "reconnect", "server", "server-nodaemon"}:
            raise ValueError("wireless_adb_command_not_permitted")
        return args

    def run_adb(self, args, timeout, check=True, capture_output=True, text=True, retry_safe=False):
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("wireless_adb_timeout_required")
        arguments = self._arguments(args)
        safe = retry_safe is True and _safe_retry(arguments)
        with self._operation() as deadline:
            if retry_safe and not safe:
                self._log("safe_retry_declined")
            while True:
                serial = self._ensure(deadline)
                command = ["-s", serial, *arguments]
                timed_out = None
                try:
                    result = self._exec(command, timeout, capture_output=capture_output, text=text, deadline=deadline)
                except (subprocess.TimeoutExpired, _ADBServerUnavailable) as error:
                    timed_out = error
                    result = None
                failed = timed_out is not None or result.returncode != 0
                if failed and safe:
                    # A command timeout/nonzero alone is not proof of disconnection.
                    try:
                        probe = self._exec(["-s", serial, "get-state"], self._budget(deadline), deadline=deadline)
                        disconnected = probe.returncode != 0 or _text(probe.stdout).strip() != "device"
                    except (subprocess.TimeoutExpired, OSError):
                        disconnected = True
                    if disconnected:
                        self.serial = None
                        self._log("safe_command_interrupted_waiting_for_transport")
                        self._budget(deadline)
                        self.sleep(self.interval if deadline is None else min(
                            self.interval, max(0, deadline - self.clock())))
                        continue
                if timed_out is not None:
                    if isinstance(timed_out, _ADBServerUnavailable):
                        raise ConnectionUnavailable("wireless_adb_server_unavailable") from None
                    raise timed_out
                if check and result.returncode:
                    raise subprocess.CalledProcessError(result.returncode, result.args,
                                                        output=result.stdout, stderr=result.stderr)
                return result


_client = None
_client_key = None
_client_lock = threading.Lock()


def _default_client():
    if not is_enabled():
        raise ConnectionUnavailable("wireless_not_enabled")
    path = os.environ.get("JINGYE_WIRELESS_CONFIG")
    if not path:
        raise ConnectionUnavailable("wireless_config_path_missing")
    with _client_lock:
        global _client, _client_key
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 1024 * 1024:
                raise ValueError()
            key = (path, hashlib.sha256(raw).digest())
            if key != _client_key:
                _client = WirelessADB(json.loads(raw))
                _client_key = key
        except (OSError, ValueError, TypeError):
            raise ConnectionUnavailable("wireless_config_unreadable_or_invalid") from None
        return _client


def ensure_connected():
    return _default_client().ensure_connected()


def run_adb(args, timeout, check=True, capture_output=True, text=True, retry_safe=False):
    return _default_client().run_adb(args, timeout, check, capture_output, text, retry_safe)
