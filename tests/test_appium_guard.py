"""Offline durable-host tests. Host identity always comes from a fake probe."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


MODULE = (Path(__file__).resolve().parents[1] / "workspace/skills/jingye-reconciliation/scripts/jingye_appium_guard.py")
URL = "http://127.0.0.1:4723"
HOST = {"pid": 12345, "createdAtUtc": "2026-11-03T08:00:00.0000000Z"}
SID = "11111111-2222-4333-8444-555555555555"


def load():
    spec = importlib.util.spec_from_file_location("guard_under_test", MODULE)
    module = importlib.util.module_from_spec(spec)
    with patch.object(subprocess, "run", side_effect=AssertionError("real host query forbidden")):
        spec.loader.exec_module(module)
    return module


class DurableAppiumGuardTest(unittest.TestCase):
    def setUp(self):
        self.module = load()
        self.temp = tempfile.TemporaryDirectory(prefix="appium-guard-offline-")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "guard.json"

    def guard(self, host=HOST, module=None):
        return (module or self.module).SessionGuard(self.path, URL, host_probe=lambda port: dict(host))

    def failure_receipt(self):
        return dict(terminalFailed=True,exclusiveRequest=True,masterNotAdded=True,
                    physicalTerminalVerified=True,processCount=0,instrumentationCount=0,ownedForwardCount=0)

    def test_failed_creation_release_persists_proof_then_permits_new_reservation(self):
        ticket=self.guard().begin();ticket.failed_creation_closed(self.failure_receipt())
        value=json.loads(self.path.read_bytes())
        self.assertIsNone(value['record']);self.assertEqual(value['lastClosed']['state'],'failed_creation_closed')
        self.guard().begin()

    def test_incomplete_failed_creation_receipt_never_clears_guard(self):
        ticket=self.guard().begin();before=self.path.read_bytes()
        for key in self.failure_receipt():
            receipt=self.failure_receipt();receipt.pop(key)
            with self.subTest(key=key),self.assertRaises(self.module.GuardError):ticket.failed_creation_closed(receipt)
            self.assertEqual(self.path.read_bytes(),before)

    def test_failed_creation_release_refuses_active_other_host_or_other_owner(self):
        ticket=self.guard().begin();ticket.created(SID)
        with self.assertRaises(self.module.GuardError):ticket.failed_creation_closed(self.failure_receipt())
        self.path.unlink();ticket=self.guard().begin()
        ticket.guard.host_probe=lambda _:{**HOST,'pid':999}
        with self.assertRaises(self.module.GuardError):ticket.failed_creation_closed(self.failure_receipt())
        ticket.guard.host_probe=lambda _:HOST;ticket.owner='0'*32
        with self.assertRaises(self.module.GuardError):ticket.failed_creation_closed(self.failure_receipt())

    def test_failed_creation_release_write_error_preserves_unresolved_marker(self):
        ticket=self.guard().begin();before=self.path.read_bytes()
        with patch.object(ticket.guard,'_write',side_effect=self.module.GuardError('write')):
            with self.assertRaises(self.module.GuardError):ticket.failed_creation_closed(self.failure_receipt())
        self.assertEqual(self.path.read_bytes(),before)

    def test_active_record_blocks_fresh_module_instance_same_host(self):
        ticket = self.guard().begin()
        ticket.created(SID)
        other = load()
        with self.assertRaises(other.GuardError) as caught:
            self.guard(module=other).begin()
        self.assertEqual(str(caught.exception), "appium_guard_same_host_unresolved")

    def test_fresh_python_process_cannot_bypass_creation_unknown(self):
        ticket = self.guard().begin()
        ticket.uncertain("creation_unknown")
        child = r"""
import importlib.util,json,sys
from unittest.mock import patch
spec=importlib.util.spec_from_file_location('fresh_guard',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
g=m.SessionGuard(sys.argv[2],'http://127.0.0.1:4723',host_probe=lambda port:json.loads(sys.argv[3]))
with patch.object(m.subprocess,'run',side_effect=AssertionError('real host query forbidden')):
 try:g.begin()
 except m.GuardError as e:print(e.code)
 else:raise SystemExit(2)
"""
        result = subprocess.run([sys.executable, "-c", child, str(MODULE), str(self.path), json.dumps(HOST)],
                                capture_output=True, text=True, timeout=15, check=True)
        self.assertEqual(result.stdout.strip(), "appium_guard_creation_outcome_unknown_across_hosts")

    def test_same_pid_different_birth_is_new_instance_and_archives_old_marker(self):
        self.guard().begin().uncertain("cleanup_uncertain")
        newer = {**HOST, "createdAtUtc": "2026-11-03T09:00:00.0000000Z"}
        self.guard(newer).begin()
        value = json.loads(self.path.read_text())
        self.assertEqual(value["record"]["host"], newer)
        self.assertEqual(value["history"][0]["previousRecord"]["host"], HOST)
        self.assertEqual(value["history"][0]["event"], "host_instance_changed")

    def test_different_pid_with_same_birth_is_distinct(self):
        self.guard().begin().created(SID)
        self.guard({**HOST, "pid": HOST["pid"] + 1}).begin()
        self.assertEqual(len(json.loads(self.path.read_text())["history"]), 1)

    def test_unfinished_creation_is_not_archived_when_host_changes(self):
        for state in ("creating", "creation_unknown"):
            with self.subTest(state=state):
                if self.path.exists():
                    self.path.unlink()
                ticket = self.guard().begin()
                if state == "creation_unknown":
                    ticket.uncertain("creation_unknown")
                previous = self.path.read_bytes()
                newer = {**HOST, "pid": 99999, "createdAtUtc": "2026-11-03T09:00:00.0000000Z"}
                with self.assertRaises(self.module.GuardError) as caught:
                    self.guard(newer).begin()
                self.assertEqual(caught.exception.code, "appium_guard_creation_outcome_unknown_across_hosts")
                self.assertEqual(self.path.read_bytes(), previous)

    def test_normal_explicit_close_clears_only_this_record(self):
        ticket = self.guard().begin()
        ticket.created(SID)
        ticket.closed()
        value = json.loads(self.path.read_text())
        self.assertIsNone(value["record"])
        self.assertEqual(value["lastClosed"]["sessionId"], SID)
        self.guard().begin()
        with self.assertRaises(self.module.GuardError):
            ticket.closed()

    def test_cleanup_uncertain_cannot_be_cleared_even_by_original_ticket(self):
        ticket = self.guard().begin()
        ticket.created(SID)
        ticket.uncertain("cleanup_uncertain")
        with self.assertRaises(self.module.GuardError):
            ticket.closed()
        with self.assertRaises(self.module.GuardError):
            self.guard().begin()

    def test_corrupt_journal_is_unchanged_and_refuses_creation(self):
        self.path.write_text("{bad json")
        with self.assertRaises(self.module.GuardError):
            self.guard().begin()
        self.assertEqual(self.path.read_text(), "{bad json")

    def test_invalid_record_or_wrong_server_refuses_creation(self):
        for value in ({"schema": 1, "server": "local:4723", "record": {}, "history": []},
                      {"schema": 1, "server": "local:9999", "record": None, "history": []}):
            with self.subTest(value=value):
                self.path.write_text(json.dumps(value))
                with self.assertRaises(self.module.GuardError):
                    self.guard().begin()

    def test_atomic_write_failure_does_not_create_or_erase_record(self):
        with patch.object(self.module.os, "replace", side_effect=OSError("offline write failure")):
            with self.assertRaises(self.module.GuardError):
                self.guard().begin()
        self.assertFalse(self.path.exists())
        ticket = self.guard().begin()
        ticket.created(SID)
        previous = self.path.read_bytes()
        with patch.object(self.module.os, "replace", side_effect=OSError("offline write failure")):
            with self.assertRaises(self.module.GuardError):
                ticket.closed()
        self.assertEqual(self.path.read_bytes(), previous)
        with self.assertRaises(self.module.GuardError):
            self.guard().begin()

    def test_query_failure_before_reservation_refuses_without_record(self):
        guard = self.module.SessionGuard(self.path, URL, host_probe=Mock(side_effect=OSError("mock CIM failure")))
        with self.assertRaises(self.module.GuardError):
            guard.begin()
        self.assertFalse(self.path.exists())

    def test_query_failure_while_marking_uncertain_sets_unbound_block(self):
        guard = self.guard()
        ticket = guard.begin()
        ticket.created(SID)
        guard.host_probe = Mock(side_effect=OSError("mock CIM failure"))
        ticket.uncertain("cleanup_uncertain")
        self.assertEqual(json.loads(self.path.read_text())["record"]["scope"], "unbound")
        with self.assertRaises(self.module.GuardError):
            self.guard({**HOST, "pid": 99999}).begin()

    def test_host_changed_during_remote_is_quarantined_for_current_host(self):
        guard = self.guard()
        ticket = guard.begin()
        newer = {**HOST, "pid": 99999}
        guard.host_probe = lambda port: newer
        with self.assertRaises(self.module.GuardError):
            ticket.created(SID)
        ticket.uncertain("creation_unknown")
        with self.assertRaises(self.module.GuardError):
            self.guard(newer).begin()
        self.assertEqual(json.loads(self.path.read_text())["record"]["host"], newer)

    def test_nonlocal_servers_and_incomplete_host_identity_are_rejected(self):
        with self.assertRaises(self.module.GuardError):
            self.module.SessionGuard(self.path, "http://192.0.2.1:4723", host_probe=lambda port: HOST)
        with self.assertRaises(self.module.GuardError):
            self.guard({"pid": HOST["pid"]}).begin()

    def test_environment_config_uses_shared_directory_or_explicit_path_without_query(self):
        config = Path(self.temp.name) / "wireless.json"
        with patch.dict(os.environ, {"JINGYE_WIRELESS_ENABLED": "1", "JINGYE_WIRELESS_CONFIG": str(config)}), \
             patch.object(self.module, "SessionGuard") as constructor:
            config.write_text("{}")
            self.module.begin(URL + "/wd/hub")
            self.assertEqual(constructor.call_args.args[0], config.parent / "appium-session-guard-4723.json")
            explicit = config.parent / "shared" / "explicit.json"
            config.write_text(json.dumps({"appium_guard_path": str(explicit)}))
            self.module.begin(URL)
            self.assertEqual(constructor.call_args.args[0], explicit)


if __name__ == "__main__":
    unittest.main()
