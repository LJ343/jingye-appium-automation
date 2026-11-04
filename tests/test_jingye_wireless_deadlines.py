"""Pinned executable and end-to-end deadline invariants, with no real I/O."""
from __future__ import annotations

import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest

from test_jingye_wireless import ADB, ADB_SHA256, FakeClock, FakeRunner, IDENTITY, OLD, load_module


class DeadlineTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory(prefix="jingye-wireless-deadline-mock-")
        self.addCleanup(self.tmp.cleanup)
        self.clock = FakeClock()
        self.runner = FakeRunner()
        self.probe_cost = 0.0
        self.probes = []
        self.config = {
            "adb_path": ADB,
            "log_path": str(Path(self.tmp.name) / "events.jsonl"),
            "expected_identity_sha256": hashlib.sha256(IDENTITY.encode()).hexdigest(),
            "wait_interval_seconds": 0.05,
            "max_wait_seconds": 5,
            "heartbeat_seconds": 0.05,
            "command_timeout_seconds": 4,
        }

    def probe(self, timeout):
        self.probes.append((self.clock(), timeout))
        self.clock.now += min(self.probe_cost, timeout)
        return 0x29

    def client(self, *, config=None, digest=ADB_SHA256):
        return self.module.WirelessADB(
            self.config if config is None else config,
            runner=self.runner, clock=self.clock, sleeper=self.clock.sleep,
            server_version_probe=self.probe, binary_digest_reader=lambda path: digest)

    def test_unapproved_executable_path_is_rejected_before_any_cli(self):
        with self.assertRaises(self.module.ConnectionUnavailable):
            client = self.client(config={**self.config, "adb_path": "C:/different-tools/adb.exe"})
            client.ensure_connected()
        self.assertEqual(self.runner.calls, [])

    def test_wrong_executable_hash_is_rejected_before_any_cli(self):
        with self.assertRaises(self.module.ConnectionUnavailable):
            client = self.client(digest="0" * 64)
            client.ensure_connected()
        self.assertEqual(self.runner.calls, [])

    def test_probe_time_is_deducted_from_cli_command_timeout(self):
        self.probe_cost = 2
        client = self.client()
        client._exec(["devices"], 5)
        self.assertAlmostEqual(self.clock(), 2)
        self.assertEqual(len(self.runner.calls), 1)
        self.assertAlmostEqual(self.runner.calls[0][1]["timeout"], 3)

    def test_positive_max_wait_also_bounds_long_business_command(self):
        self.probe_cost = 0.3
        client = self.client()
        seen = []

        def observe(argv, serial, command, kwargs):
            seen.append((self.clock(), kwargs["timeout"]))
            return subprocess.CompletedProcess(argv, 0, "", "")

        self.runner.business_handler = observe
        client.run_adb(["pull", "/mock.pdf", "local.part"], 150)
        self.assertEqual(len(seen), 1)
        at, timeout = seen[0]
        self.assertLess(timeout, 150)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(at + timeout, client.max_wait + 1e-9)

    def test_probe_consuming_whole_command_budget_prevents_cli(self):
        self.probe_cost = 5
        client = self.client()
        with self.assertRaises((self.module.ConnectionUnavailable, subprocess.TimeoutExpired)):
            client._exec(["devices"], 1)
        self.assertAlmostEqual(self.clock(), 1)
        self.assertEqual(self.runner.calls, [])

    def test_unlimited_connection_wait_keeps_each_business_call_bounded(self):
        self.probe_cost = 0.5
        client = self.client(config={**self.config, "max_wait_seconds": 0})
        client.run_adb(["pull", "/mock.pdf", "local.part"], 150)
        self.assertEqual(len(self.runner.business), 1)
        timeout = self.runner.business[0][2]["timeout"]
        self.assertGreater(timeout, 0)
        self.assertAlmostEqual(timeout, 149.5)

    def test_safe_retry_cannot_reset_overall_positive_wait_budget(self):
        self.probe_cost = 0.01
        client = self.client(config={**self.config, "max_wait_seconds": 0.4})
        attempts = []

        def disconnect(argv, serial, command, kwargs):
            attempts.append((self.clock(), kwargs["timeout"]))
            self.runner.online = {}
            self.clock.on_sleep = lambda: self.runner.online.update({OLD: IDENTITY})
            return subprocess.CompletedProcess(argv, 1, "", "")

        self.runner.business_handler = disconnect
        with self.assertRaises(self.module.ConnectionUnavailable):
            client.run_adb(["pull", "/mock.pdf", "local.part"], 150, retry_safe=True)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertGreaterEqual(self.clock(), client.max_wait - 1e-9)
        self.assertLessEqual(self.clock(), client.max_wait + 1e-9)
        self.assertTrue(all(at + timeout <= client.max_wait + 1e-9 for at, timeout in attempts))
        self.assertLess(attempts[-1][1], attempts[0][1])


if __name__ == "__main__":
    unittest.main()
