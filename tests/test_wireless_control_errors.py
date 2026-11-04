"""Fatal transport control errors must cross discovery without extra probes."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

from test_wireless_discovery import Clock, Network, discovery, HOST


SOURCE = (Path(__file__).resolve().parents[1] / "workspace" / "skills" /
          "jingye-reconciliation" / "scripts" / "jingye_wireless.py")
SPEC = importlib.util.spec_from_file_location("wireless_control_error_class", SOURCE)
wireless = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wireless)


class FatalControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock.now = 1000
        self.network = Network(self.clock, default="timeout")
        for item in (
            patch.object(discovery.time, "monotonic", self.clock.monotonic),
            patch.object(discovery.socket, "socket", self.network.socket),
            patch.object(discovery.selectors, "DefaultSelector", self.network.selector),
        ):
            item.start()
            self.addCleanup(item.stop)

    def make(self):
        return discovery.EndpointDiscovery(dict(enabled=True, allowed_ipv4=[HOST],
            port_range=[37000, 37010], max_concurrency=4, connect_timeout_s=.2,
            total_deadline_s=1, scan_cooldown_s=60))

    def test_cached_control_errors_propagate_without_scanning(self):
        for code in ("wireless_log_unavailable", "wireless_adb_server_version_mismatch",
                     "wireless_wait_budget_exhausted", "wireless_adb_binary_hash_mismatch"):
            with self.subTest(code=code):
                helper = self.make()
                error = wireless.ConnectionUnavailable(code)
                def verify(endpoint, remaining):
                    raise error
                with self.assertRaises(wireless.ConnectionUnavailable) as caught:
                    helper.discover(cached_endpoint=f"{HOST}:42029", verify_candidate=verify)
                self.assertIs(caught.exception, error)
                self.assertEqual(self.network.connections, [])
                self.assertEqual(helper.last_report["reason"], "fatal_verifier_error")
                self.assertEqual(helper.last_report["verification_errors"], 0)
                self.assertFalse(helper._call_lock.locked())

    def test_fatal_scan_verifier_closes_all_inflight_probes(self):
        helper = self.make()
        self.network.modes[37000] = "pending"
        error = wireless.ConnectionUnavailable("wireless_adb_server_version_mismatch")
        def verify(endpoint, remaining):
            raise error
        with self.assertRaises(wireless.ConnectionUnavailable) as caught:
            helper.discover(verify_candidate=verify)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.network.active, 0)
        self.assertTrue(all(probe.closed for probe in self.network.probes))
        self.assertLessEqual(len(self.network.connections), 4)
        self.assertEqual(helper.last_report["reason"], "fatal_verifier_error")
        self.assertFalse(helper._call_lock.locked())


if __name__ == "__main__":
    unittest.main()
