"""Mock-only discovery tests: no network, ADB, Appium, or business imports."""

import errno
import importlib.util
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch


SCRIPT = (Path(__file__).resolve().parents[1] / "workspace/skills/jingye-reconciliation/scripts" / "jingye_wireless_discovery.py")
SPEC = importlib.util.spec_from_file_location("discovery_under_test", SCRIPT)
discovery = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = discovery
SPEC.loader.exec_module(discovery)
HOST = "192.168.50.53"


class Clock:
    now = 1000.0

    def monotonic(self):
        return self.now


class Network:
    def __init__(self, clock, modes=None, default="refused"):
        self.clock = clock
        self.modes = modes or {}
        self.default = default
        self.probes = []
        self.connections = []
        self.active = 0
        self.peak = 0

    def socket(self, family, kind):
        self.active += 1
        self.peak = max(self.peak, self.active)
        probe = Probe(self)
        self.probes.append(probe)
        return probe

    def selector(self):
        return Selector(self)


class Probe:
    def __init__(self, network):
        self.network = network
        self.closed = False
        self.mode = None

    def setblocking(self, value):
        assert value is False

    def connect_ex(self, address):
        self.network.connections.append(address)
        self.mode = self.network.modes.get(address[1], self.network.default)
        return {"open": 0, "pending": errno.EINPROGRESS,
                "timeout": errno.EINPROGRESS, "refused": errno.ECONNREFUSED}[self.mode]

    def getsockopt(self, level, option):
        assert not self.closed
        return 0

    def close(self):
        if not self.closed:
            self.closed = True
            self.network.active -= 1


class Selector:
    def __init__(self, network):
        self.network = network
        self.registered = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        assert not self.registered

    def register(self, probe, event):
        self.registered[probe] = event

    def unregister(self, probe):
        if probe not in self.registered:
            raise KeyError(probe)
        del self.registered[probe]

    def select(self, timeout):
        self.network.clock.now += max(0.000001, timeout)
        return [(type("Key", (), {"fileobj": probe})(), event)
                for probe, event in list(self.registered.items()) if probe.mode == "pending"]


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.clock.now = 1000.0
        self.network = Network(self.clock)
        self.patches = [
            patch.object(discovery.time, "monotonic", self.clock.monotonic),
            patch.object(discovery.socket, "socket", self.network.socket),
            patch.object(discovery.selectors, "DefaultSelector", self.network.selector),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.assertEqual(self.network.active, 0)

    def make(self, **overrides):
        config = dict(enabled=True, allowed_ipv4=[HOST], port_range=[37000, 37010],
                      max_concurrency=4, connect_timeout_s=0.2,
                      total_deadline_s=1, scan_cooldown_s=60)
        config.update(overrides)
        return discovery.EndpointDiscovery(config)

    def test_rejects_unsafe_config(self):
        for override in ({"allowed_ipv4": []}, {"allowed_ipv4": ["192.168.50.0/24"]},
                         {"allowed_ipv4": ["localhost"]}, {"allowed_ipv4": ["127.0.0.1"]},
                         {"max_concurrency": 129}, {"scan_cooldown_s": 59},
                         {"port_range": [1, 65535]}, {"connect_timeout_s": float("nan")},
                         {"unexpected": True}):
            with self.subTest(override=override), self.assertRaises(ValueError):
                self.make(**override)

    def test_cached_success_never_scans(self):
        calls = []
        helper = self.make()
        endpoint = f"{HOST}:42029"
        result = helper.discover(endpoint, [f"{HOST}:42030"],
                                 lambda candidate, budget: calls.append((candidate, budget)) or True)
        self.assertEqual(result, endpoint)
        self.assertEqual([item[0] for item in calls], [endpoint])
        self.assertEqual(self.network.connections, [])
        self.assertEqual(helper.last_report["reason"], "verified_cached")

    def test_mdns_rejects_other_hosts_and_deduplicates(self):
        calls = []
        endpoint = f"{HOST}:42029"
        helper = self.make()
        result = helper.discover(endpoint, ["192.168.50.54:42029", endpoint,
                                            f"{HOST}:42030", f"{HOST}:42030"],
                                 lambda candidate, budget: calls.append(candidate) or candidate.endswith(":42030"))
        self.assertEqual(result, f"{HOST}:42030")
        self.assertEqual(calls, [endpoint, f"{HOST}:42030"])
        self.assertEqual(self.network.connections, [])
        self.assertEqual(helper.last_report["rejected_endpoints"], 1)

    def test_disabled_default_does_not_open_sockets(self):
        helper = discovery.EndpointDiscovery({})
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: True))
        self.assertEqual(helper.last_report["reason"], "disabled")
        self.assertEqual(self.network.connections, [])

    def test_bounded_parallel_scan_stops_and_closes_all_on_identity_success(self):
        self.network.default = "timeout"
        self.network.modes[37003] = "pending"
        helper = self.make()
        calls = []
        result = helper.discover(verify_candidate=lambda endpoint, budget: calls.append(endpoint) or True)
        self.assertEqual(result, f"{HOST}:37003")
        self.assertEqual(calls, [f"{HOST}:37003"])
        self.assertEqual(len(self.network.connections), 4)
        self.assertLessEqual(self.network.peak, 4)
        self.assertTrue(all(probe.closed for probe in self.network.probes))
        self.assertEqual({host for host, port in self.network.connections}, {HOST})

    def test_other_open_service_is_not_returned_without_identity(self):
        self.network.modes = {37001: "pending", 37003: "pending"}
        helper = self.make()
        calls = []
        result = helper.discover(verify_candidate=lambda endpoint, budget:
                                 calls.append(endpoint) or endpoint.endswith(":37003"))
        self.assertEqual(result, f"{HOST}:37003")
        self.assertEqual(calls, [f"{HOST}:37001", f"{HOST}:37003"])

    def test_deadline_closes_pending_and_does_not_finish_whole_range(self):
        self.network.default = "timeout"
        helper = self.make(port_range=[37000, 37100], total_deadline_s=0.3)
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: False))
        self.assertEqual(helper.last_report["reason"], "deadline")
        self.assertLess(self.network.active, 1)
        self.assertLess(len(self.network.connections), 101)
        self.assertLessEqual(self.network.peak, 4)
        self.assertLessEqual(helper.last_report["elapsed_s"], 0.30001)

    def test_parent_budget_and_zero_budget(self):
        self.network.default = "timeout"
        helper = self.make()
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: True, max_seconds=0))
        self.assertEqual(self.network.connections, [])
        self.assertEqual(helper.last_report["reason"], "deadline")
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: True, max_seconds=0.1))
        self.assertLessEqual(helper.last_report["elapsed_s"], 0.10001)

    def test_cooldown_is_measured_after_scan_end_and_cache_still_works(self):
        helper = self.make()
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: False))
        self.assertEqual(helper.last_report["reason"], "scan_complete")
        previous_count = len(self.network.connections)
        self.clock.now += 59
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: False))
        self.assertEqual(helper.last_report["reason"], "cooldown")
        self.assertEqual(len(self.network.connections), previous_count)
        endpoint = f"{HOST}:42029"
        self.assertEqual(helper.discover(endpoint, verify_candidate=lambda endpoint, budget: True), endpoint)
        self.clock.now += 1
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: False))
        self.assertEqual(helper.last_report["reason"], "scan_complete")
        self.assertGreater(len(self.network.connections), previous_count)

    def test_candidate_limit_is_explicit_not_false_identity_success(self):
        self.network.default = "open"
        helper = self.make(max_verify_candidates=2)
        calls = []
        result = helper.discover(verify_candidate=lambda endpoint, budget: calls.append(endpoint) or False)
        self.assertIsNone(result)
        self.assertEqual(len(calls), 2)
        self.assertEqual(helper.last_report["reason"], "candidate_limit")

    def test_cancelled_before_and_during_scan_closes_sockets(self):
        event = threading.Event()
        event.set()
        helper = self.make()
        self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: True, cancel_event=event))
        self.assertEqual(self.network.connections, [])
        self.assertEqual(helper.last_report["reason"], "cancelled")
        event.clear()
        self.network.default = "timeout"
        self.network.modes[37001] = "pending"

        def reject_and_cancel(endpoint, budget):
            event.set()
            return False

        self.assertIsNone(helper.discover(verify_candidate=reject_and_cancel, cancel_event=event))
        self.assertEqual(helper.last_report["reason"], "cancelled")
        self.assertEqual(self.network.active, 0)

    def test_verifier_errors_and_overrun_do_not_accept_candidate(self):
        helper = self.make(enabled=False)

        def failure(endpoint, budget):
            raise OSError("mock-only failure")

        self.assertIsNone(helper.discover(f"{HOST}:42029", verify_candidate=failure))
        self.assertEqual(helper.last_report["verification_errors"], 1)

        def overrun(endpoint, budget):
            self.assertLessEqual(budget, 0.1)
            self.clock.now += budget + 0.01
            return True

        self.assertIsNone(helper.discover(f"{HOST}:42029", verify_candidate=overrun, max_seconds=0.1))
        self.assertEqual(helper.last_report["reason"], "deadline")

    def test_socket_exhaustion_closes_existing_pending_probes(self):
        self.network.default = "timeout"
        factory = self.network.socket

        def exhausted(family, kind):
            if len(self.network.probes) == 2:
                raise OSError("mock socket exhaustion")
            return factory(family, kind)

        helper = self.make()
        with patch.object(discovery.socket, "socket", exhausted):
            self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: True))
        self.assertEqual(helper.last_report["reason"], "probe_error")
        self.assertTrue(all(probe.closed for probe in self.network.probes))

    def test_selector_network_error_closes_all_pending_probes(self):
        self.network.default = "timeout"
        helper = self.make()
        with patch.object(Selector, "select", side_effect=OSError("mock network down")):
            self.assertIsNone(helper.discover(verify_candidate=lambda endpoint, budget: True))
        self.assertEqual(helper.last_report["reason"], "probe_error")
        self.assertTrue(all(probe.closed for probe in self.network.probes))

    def test_partial_deadlines_resume_until_high_port_without_prefix_starvation(self):
        self.network.default = "timeout"
        self.network.modes[37090] = "pending"
        helper = self.make(port_range=[37000, 37100], total_deadline_s=0.3)
        result = None
        starts = []
        for _ in range(20):
            before = len(self.network.connections)
            result = helper.discover(verify_candidate=lambda endpoint, budget: endpoint.endswith(":37090"))
            starts.append(self.network.connections[before][1])
            self.assertNotEqual(helper.last_report["reason"], "cooldown")
            if result:
                break
        self.assertEqual(result, f"{HOST}:37090")
        self.assertEqual(starts, sorted(set(starts)))
        self.assertLess(self.clock.now - 1000, 10)

    def test_deadline_inflight_probes_are_retried_and_only_complete_sweep_cools(self):
        self.network.default = "timeout"
        helper = self.make(port_range=[37000, 37012], total_deadline_s=0.3)
        for _ in range(20):
            helper.discover(verify_candidate=lambda endpoint, budget: False)
            if helper.last_report["scan_complete"]:
                break
            self.assertNotEqual(helper.last_report["reason"], "cooldown")
            self.assertIsNone(helper._last_scan_finished)
        self.assertTrue(helper.last_report["scan_complete"])
        self.assertEqual(helper.last_report["scan_remaining"], 0)
        ports = [port for _, port in self.network.connections]
        self.assertTrue(set(range(37000, 37013)).issubset(ports))
        self.assertGreater(len(ports), 13)  # unfinished, not timed-out, connects were retained
        before = len(ports)
        helper.discover(verify_candidate=lambda endpoint, budget: False)
        self.assertEqual(helper.last_report["reason"], "cooldown")
        self.assertEqual(len(self.network.connections), before)

    def test_cancelled_scan_resumes_fresh_ports_and_preserves_cancelled_inflight(self):
        self.network.default = "timeout"
        self.network.modes[37000] = "pending"
        event = threading.Event()
        helper = self.make()
        def cancel(endpoint, budget):
            event.set()
            return False
        helper.discover(verify_candidate=cancel, cancel_event=event)
        self.assertEqual(helper.last_report["reason"], "cancelled")
        first_ports = [port for _, port in self.network.connections]
        event.clear()
        self.network.default = "refused"
        self.network.modes = {port: "refused" for port in range(37000, 37011)}
        helper.discover(verify_candidate=lambda endpoint, budget: False, cancel_event=event)
        following = [port for _, port in self.network.connections[len(first_ports):]]
        self.assertEqual(following[0], 37004)
        self.assertTrue(set(first_ports[1:]).issubset(following))
        self.assertTrue(helper.last_report["scan_complete"])

    def test_verifier_global_budget_edge_retains_open_candidate_for_later_slice(self):
        self.network.default = "refused"
        self.network.modes[37000] = "pending"
        helper = self.make(total_deadline_s=0.3)
        def exhausted(endpoint, budget):
            self.clock.now += budget + 0.01
            return False
        helper.discover(verify_candidate=exhausted)
        self.assertEqual(helper.last_report["reason"], "deadline")
        result = helper.discover(verify_candidate=lambda endpoint, budget: endpoint.endswith(":37000"))
        self.assertEqual(result, f"{HOST}:37000")
        self.assertGreaterEqual([port for _, port in self.network.connections].count(37000), 2)

    def test_synchronous_verifier_does_not_discard_other_ready_tcp_candidates(self):
        self.network.modes = {37000: "pending", 37001: "pending"}
        helper = self.make(port_range=[37000, 37002], max_concurrency=2)
        def slow_rejection(endpoint, budget):
            if endpoint.endswith(":37000"):
                self.clock.now += 0.4  # exceeds the other socket's connect timeout
                return False
            return endpoint.endswith(":37001")
        result = helper.discover(verify_candidate=slow_rejection)
        self.assertEqual(result, f"{HOST}:37001")

    def test_each_verifier_has_own_budget_and_other_open_service_cannot_consume_scan(self):
        self.network.modes = {37000: "open", 37010: "open"}
        helper = self.make(verify_timeout_s=0.1)
        budgets = []
        def verify(endpoint, budget):
            budgets.append(budget)
            if endpoint.endswith(":37000"):
                self.clock.now += budget
                return False
            return endpoint.endswith(":37010")
        self.assertEqual(helper.discover(verify_candidate=verify), f"{HOST}:37010")
        self.assertTrue(all(0 < budget <= 0.1 for budget in budgets))
        self.assertEqual(helper.last_report["verified_candidates"], 2)  # attempts, not two accepted identities

    def test_candidate_limit_partial_scan_resumes_without_cooldown(self):
        self.network.modes = {37000: "open", 37001: "open", 37005: "open"}
        helper = self.make(max_verify_candidates=1)
        helper.discover(verify_candidate=lambda endpoint, budget: False)
        self.assertEqual(helper.last_report["reason"], "candidate_limit")
        self.assertFalse(helper.last_report["scan_complete"])
        result = helper.discover(verify_candidate=lambda endpoint, budget: endpoint.endswith(":37005"))
        self.assertEqual(result, f"{HOST}:37005")

    def test_full_ephemeral_sized_range_reaches_last_port_across_tiny_slices(self):
        target = 60999
        self.network.modes[target] = "open"
        helper = self.make(port_range=[32768, target], total_deadline_s=0.05)
        original = Probe.connect_ex
        def timed_connect(probe, address):
            result = original(probe, address)
            self.clock.now += 0.0005
            return result
        with patch.object(Probe, "connect_ex", timed_connect):
            for _ in range(300):
                result = helper.discover(verify_candidate=lambda endpoint, budget: endpoint.endswith(f":{target}"))
                if result:
                    break
        self.assertEqual(result, f"{HOST}:{target}")
        ports = [port for _, port in self.network.connections]
        self.assertEqual(len(ports), target - 32768 + 1)
        self.assertEqual(ports, list(range(32768, target + 1)))


if __name__ == "__main__":
    unittest.main(verbosity=2)
