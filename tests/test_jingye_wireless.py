"""Transport-boundary tests. Every subprocess call is replaced by a fake."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE_PATH = (Path(__file__).resolve().parents[1] / "workspace/skills/jingye-reconciliation/scripts" / "jingye_wireless.py")
OLD = "192.0.2.10:41001"
NEW = "192.0.2.10:41002"
IDENTITY = "mock-owned-phone"
ADB = str(Path("C:/Users/YOUR_USER/adb/platform-tools/adb.exe"))
ADB_SHA256 = "957e46b8615f7af5b7292a2ddabe98d2e61940c3fb2b0545756507f080613e71"
PREFIX = [ADB, "-H", "127.0.0.1", "-P", "5037"]


def load_module():
    spec = importlib.util.spec_from_file_location("_jingye_wireless_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.object(subprocess, "run", side_effect=AssertionError("real subprocess forbidden")), \
         mock.patch.object(subprocess, "Popen", side_effect=AssertionError("real subprocess forbidden")):
        spec.loader.exec_module(module)
    return module


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        if seconds <= 0:
            raise AssertionError("non-positive fake sleep")
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep()


class FakeRunner:
    def __init__(self):
        self.online = {OLD: IDENTITY}
        self.connectable = {}
        self.mdns = ""
        self.calls = []
        self.business = []
        self.business_handler = None

    def __call__(self, argv, **kwargs):
        if argv[:5] != PREFIX:
            raise AssertionError("ADB server selection or executable changed")
        if kwargs.get("check") is not False:
            raise AssertionError("helper must inspect return codes itself")
        if not isinstance(kwargs.get("timeout"), (int, float)) or kwargs["timeout"] <= 0:
            raise AssertionError("every command needs an explicit positive timeout")
        if kwargs.get("stdin") != subprocess.DEVNULL:
            raise AssertionError("ADB must not read interactive input")
        self.calls.append((list(argv), dict(kwargs)))
        args = argv[5:]
        result = lambda code=0, out="": subprocess.CompletedProcess(argv, code, out, "")
        if args == ["devices"]:
            return result(out="List of devices attached\n" + "".join(
                serial + "\tdevice\n" for serial in self.online))
        if args == ["mdns", "services"]:
            return result(out=self.mdns)
        if args[:1] == ["connect"]:
            if args[1] in self.connectable:
                self.online[args[1]] = self.connectable[args[1]]
            return result(0 if args[1] in self.online else 1)
        if len(args) < 3 or args[0] != "-s":
            raise AssertionError("unexpected fake ADB command")
        serial, command = args[1], args[2:]
        if command == ["get-state"]:
            return result(0 if serial in self.online else 1,
                          "device\n" if serial in self.online else "offline\n")
        if command == ["shell", "getprop", "ro.serialno"]:
            return result(0 if serial in self.online else 1, self.online.get(serial, ""))
        self.business.append((serial, command, dict(kwargs)))
        if self.business_handler:
            return self.business_handler(argv, serial, command, kwargs)
        return result(out="mock-result")


class WirelessTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix="jingye-wireless-mock-")
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.runner = FakeRunner()
        self.client = self.module.WirelessADB({
            "adb_path": ADB,
            "log_path": str(Path(self.tmp.name) / "events.jsonl"),
            "expected_identity_sha256": hashlib.sha256(IDENTITY.encode()).hexdigest(),
            "wait_interval_seconds": 0.05,
            "max_wait_seconds": 0.2,
            "heartbeat_seconds": 0.05,
            "command_timeout_seconds": 0.1,
        }, runner=self.runner, clock=self.clock, sleeper=self.clock.sleep,
           server_version_probe=lambda timeout: 0x29,
           binary_digest_reader=lambda path: ADB_SHA256)

    def test_import_and_disabled_default_never_run_subprocess(self):
        module = load_module()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(subprocess, "run", side_effect=AssertionError("subprocess forbidden")):
            self.assertFalse(module.is_enabled())
            with self.assertRaises(module.ConnectionUnavailable) as caught:
                module.ensure_connected()
            self.assertEqual(caught.exception.code, "wireless_not_enabled")

    def test_new_endpoint_replaces_stale_s_argument(self):
        self.assertEqual(self.client.ensure_connected(), OLD)
        self.runner.online = {NEW: IDENTITY}
        result = self.client.run_adb(["adb", "-s", OLD, "shell", "stat", "/mock.pdf"], 7)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.runner.business[0][:2], (NEW, ["shell", "stat", "/mock.pdf"]))
        self.assertGreater(self.runner.business[0][2]["timeout"], 0)
        self.assertLessEqual(self.runner.business[0][2]["timeout"], self.client.max_wait)

    def test_wrong_identity_never_executes_requested_command(self):
        self.runner.online = {OLD: "different-mock-phone"}
        with self.assertRaises(self.module.ConnectionUnavailable):
            self.client.run_adb(["pull", "/mock.pdf", "local.part"], 7, retry_safe=True)
        self.assertEqual(self.runner.business, [])
        self.assertTrue(self.clock.sleeps)

    def interrupted_handler(self, *, timeout):
        def handle(argv, serial, command, kwargs):
            if len(self.runner.business) == 1:
                self.runner.online = {}
                self.clock.on_sleep = lambda: self.runner.online.update({NEW: IDENTITY})
                if timeout:
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "mock-result", "")
        return handle

    def test_safe_pull_waits_then_retries_on_new_endpoint(self):
        self.runner.business_handler = self.interrupted_handler(timeout=False)
        result = self.client.run_adb(["pull", "/mock.pdf", "local.part"], 7, retry_safe=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([row[0] for row in self.runner.business], [OLD, NEW])
        self.assertTrue(self.clock.sleeps)

    def test_online_shell_error_is_not_retried(self):
        self.runner.business_handler = lambda argv, *_: subprocess.CompletedProcess(argv, 1, "", "")
        with self.assertRaises(subprocess.CalledProcessError):
            self.client.run_adb(["shell", "stat", "/missing.pdf"], 7, retry_safe=True)
        self.assertEqual(len(self.runner.business), 1)
        self.assertFalse(self.clock.sleeps)

    def test_online_timeout_is_not_retried(self):
        def timeout(argv, serial, command, kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        self.runner.business_handler = timeout
        with self.assertRaises(subprocess.TimeoutExpired):
            self.client.run_adb(["pull", "/mock.pdf", "local.part"], 7, retry_safe=True)
        self.assertEqual(len(self.runner.business), 1)
        self.assertFalse(self.clock.sleeps)

    def test_disconnected_timeout_waits_then_recovers(self):
        self.runner.business_handler = self.interrupted_handler(timeout=True)
        result = self.client.run_adb(["pull", "/mock.pdf", "local.part"], 7, retry_safe=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual([row[0] for row in self.runner.business], [OLD, NEW])
        self.assertTrue(self.clock.sleeps)

    def test_unsafe_shell_is_not_replayed_even_if_disconnected(self):
        self.runner.business_handler = self.interrupted_handler(timeout=False)
        with self.assertRaises(subprocess.CalledProcessError):
            self.client.run_adb(["shell", "echo probe > /mock.txt"], 7, retry_safe=True)
        self.assertEqual(len(self.runner.business), 1)
        self.assertFalse(self.clock.sleeps)

    def test_mdns_connect_service_is_used_but_pairing_service_is_ignored(self):
        pairing_endpoint = "192.0.2.10:41003"
        self.runner.online = {}
        self.runner.connectable = {NEW: IDENTITY}
        self.runner.mdns = (
            "owned-pair _adb-tls-pairing._tcp " + pairing_endpoint + "\n"
            "owned-connect _adb-tls-connect._tcp " + NEW + "\n")
        self.assertEqual(self.client.ensure_connected(), NEW)
        connect_commands = [argv[5:] for argv, _ in self.runner.calls
                            if argv[5:6] == ["connect"]]
        self.assertEqual(connect_commands, [["connect", NEW]])
        self.assertFalse(any(pairing_endpoint in argv for argv, _ in self.runner.calls))
        self.assertEqual(self.runner.business, [])

    def test_unlimited_wait_emits_heartbeats_then_recovers_without_business(self):
        self.client.max_wait = 0
        self.runner.online = {}

        def reconnect_after_three_sleeps():
            if len(self.clock.sleeps) >= 3:
                self.runner.online = {NEW: IDENTITY}

        self.clock.on_sleep = reconnect_after_three_sleeps
        self.assertEqual(self.client.ensure_connected(), NEW)
        self.assertGreaterEqual(len(self.clock.sleeps), 3)
        records = [json.loads(line) for line in self.client.log_path.read_text(encoding="utf-8").splitlines()]
        waiting = [row for row in records if row["event"] == "waiting_for_connection"]
        self.assertGreaterEqual(len(waiting), 2)
        self.assertTrue(all(row["maxWaitSeconds"] == 0 for row in waiting))
        self.assertEqual(self.runner.business, [])

    def test_same_serial_with_changed_identity_cannot_run_subsequent_command(self):
        self.client.run_adb(["shell", "stat", "/mock.pdf"], 7)
        self.assertEqual(len(self.runner.business), 1)
        self.runner.online = {OLD: "different-mock-phone"}
        with self.assertRaises(self.module.ConnectionUnavailable):
            self.client.run_adb(["pull", "/mock.pdf", "local.part"], 7, retry_safe=True)
        self.assertEqual(len(self.runner.business), 1)

    def test_old_adb_server_version_never_starts_cli(self):
        client = self.module.WirelessADB(
            self.client.config, runner=self.runner, clock=self.clock, sleeper=self.clock.sleep,
            server_version_probe=lambda timeout: 0x28,
            binary_digest_reader=lambda path: ADB_SHA256)
        with self.assertRaises(self.module.ConnectionUnavailable) as caught:
            client.ensure_connected()
        self.assertEqual(caught.exception.code, "wireless_adb_server_version_mismatch")
        self.assertEqual(self.runner.calls, [])

    def test_unavailable_adb_server_waits_to_budget_without_starting_cli(self):
        def unavailable(timeout):
            raise OSError("mock unavailable server")
        client = self.module.WirelessADB(
            self.client.config, runner=self.runner, clock=self.clock, sleeper=self.clock.sleep,
            server_version_probe=unavailable,
            binary_digest_reader=lambda path: ADB_SHA256)
        with self.assertRaises(self.module.ConnectionUnavailable):
            client.ensure_connected()
        self.assertGreaterEqual(self.clock(), client.max_wait)
        self.assertTrue(self.clock.sleeps)
        self.assertEqual(self.runner.calls, [])


if __name__ == "__main__":
    unittest.main()
