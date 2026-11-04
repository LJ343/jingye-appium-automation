"""Real connection ownership module; all host/ADB effects are offline fakes."""
from __future__ import annotations

from contextlib import nullcontext
import importlib.util
from pathlib import Path
import shlex
from types import SimpleNamespace
import unittest

SOURCE = Path(__file__).resolve().parents[1] / "scripts/jingye_appium_ownership.py"
spec = importlib.util.spec_from_file_location("phone_ownership_under_test", SOURCE)
owner = importlib.util.module_from_spec(spec); spec.loader.exec_module(owner)
PACKAGE = "io.appium.uiautomator2.server"
HOST = {"pid": 42, "createdAtUtc": "2026-11-04T00:00:00.000Z"}
SID = "11111111-2222-4333-8444-555555555555"


def report(active=False, instrument=None):
    section = ""
    if active:
        section = "  Active instrumentation:\n    Instrumentation #0: " + (
            instrument or "ActiveInstrumentation{abc {" + PACKAGE + ".test/Runner} 1 procs}") + "\n"
    return ("ACTIVITY MANAGER RUNNING PROCESSES (dumpsys activity processes)\n" + section
            + "  Total persistent processes: 1\n"
            "  mProcessesReady=true mSystemReady=true mBooted=true mFactoryTest=0\n"
            "  mForceBackgroundCheck=false\n")


def stat(pid, ticks):
    return f"{pid} (app process) " + " ".join(["S"] + ["0"] * 18 + [str(ticks)] + ["0"] * 4)


class FakeClient:
    def __init__(self):
        self.now = 0; self.serial = "old-endpoint"; self.active = False
        self.pid = 123; self.uid = 10042; self.ticks = 777
        self.calls = []; self.stops = []; self.keep_forward = False
        self.instrument = None; self.stop_error = False; self.stop_lost = False
        self.unknown_report = False; self.bad_cmdline = False; self.bad_uid = False
        self.finished_transition = False; self.transition_reads = 0
    def _operation(self): return nullcontext(300)
    def clock(self): return self.now
    def sleep(self, seconds): self.now += seconds
    def _ensure(self, deadline):
        self.calls.append(("identity", self.serial, deadline)); return self.serial
    def _exec(self, args, timeout, deadline):
        self.calls.append((list(args), timeout, deadline)); tail = args[2:]
        out = ""; rc = 0
        if tail == ["shell", "ps", "-A", "-o", "PID,UID,NAME"]:
            out = "PID UID NAME\n" + (f"{self.pid} {self.uid} {PACKAGE}\n" if self.active else "")
        elif tail == ["shell", "dumpsys", "activity", "processes"]:
            if self.finished_transition and self.stops and not self.active and self.transition_reads == 0:
                self.transition_reads += 1
                out = report(True, "ActiveInstrumentation{abc {" + PACKAGE + ".test/Runner} FINISHED 0 procs}")
            else:
                out = "Unknown command: instrumentation\n" if self.unknown_report else report(self.active, self.instrument)
        elif tail == ["forward", "--list"]:
            out = "\n"
            if (self.active and self.serial == "old-endpoint") or self.keep_forward:
                out = "old-endpoint tcp:8200 tcp:6790\n"
        elif tail[:4] == ["shell", "run-as", PACKAGE, "id"]:
            out = str(self.uid + int(self.bad_uid)) + "\n"
        elif tail[:4] == ["shell", "run-as", PACKAGE, "cat"]:
            out = ("wrong\0" if self.bad_cmdline else PACKAGE + "\0") if tail[4].endswith("/cmdline") else stat(self.pid, self.ticks)
        elif tail[:5] == ["shell", "run-as", PACKAGE, "sh", "-c"]:
            self.stops.append(shlex.split(tail[5])[0])
            if self.stop_lost: raise TimeoutError("lost signal response")
            if self.stop_error: rc = 44
            else: self.active = False; out = "OWNED_SIGNAL_SENT\n"
        else: raise AssertionError("unexpected offline command shape")
        return SimpleNamespace(returncode=rc, stdout=out)


def make():
    client = FakeClient()
    link = SimpleNamespace(_default_client=lambda: client)
    value = owner.prepare(link, "http://127.0.0.1:4723", HOST, host_probe=lambda port: HOST)
    return value, client


def captured():
    value, client = make(); client.active = True; value.capture(SID)
    return value, client


class PhoneOwnershipTests(unittest.TestCase):
    def test_non_target_system_process_name_may_contain_spaces(self):
        text = f"PID UID NAME\n100 1000 legitimate system process name\n123 10042 {PACKAGE}\n"
        self.assertEqual(owner.parse_processes(text), [{"pid": 123, "uid": 10042, "name": PACKAGE}])
        with self.assertRaisesRegex(owner.OwnershipError, "process_name_unknown"):
            owner.parse_processes(f"PID UID NAME\n123 10042 {PACKAGE} unexpected suffix\n")

    def test_real_snapshot_capture_changed_endpoint_precise_stop_and_terminal(self):
        value, client = captured(); client.serial = "new-endpoint"
        value.finish_after_quit(SID)
        self.assertEqual(len(client.stops), 1)
        script = client.stops[0]
        self.assertLess(script.index("/stat"), script.index("kill -TERM 123"))
        self.assertLess(script.index("/cmdline"), script.index("kill -TERM 123"))
        self.assertIn('[ "$1" = 777 ]', script)
        self.assertIn('[ "$(id -u)" = 10042 ]', script)
        self.assertNotIn("force-stop", script); self.assertNotIn("kill -9", script)
        self.assertFalse(client.active)
        self.assertTrue(all(call[-1] in {60, 300} for call in client.calls if isinstance(call[0], list)))

    def test_baseline_live_or_unknown_instrumentation_cannot_start(self):
        client = FakeClient(); client.active = True
        link = SimpleNamespace(_default_client=lambda: client)
        with self.assertRaisesRegex(owner.OwnershipError, "phone_not_idle_before_remote"):
            owner.prepare(link, "http://127.0.0.1:4723", HOST, host_probe=lambda port: HOST)
        client.active = False; client.unknown_report = True
        with self.assertRaises(owner.OwnershipError):
            owner.prepare(link, "http://127.0.0.1:4723", HOST, host_probe=lambda port: HOST)
        self.assertEqual(client.stops, [])

    def test_pid_reuse_or_uid_starttime_change_never_sends_stop(self):
        for field in ["pid", "uid", "ticks"]:
            value, client = captured(); client.serial = "new-endpoint"
            setattr(client, field, getattr(client, field) + 1)
            with self.assertRaisesRegex(owner.OwnershipError, "process_identity_changed"):
                value.finish_after_quit(SID)
            self.assertEqual(client.stops, [])

    def test_instrumentation_identity_change_cannot_stop_process(self):
        value, client = captured(); client.serial = "new-endpoint"
        client.instrument = "ActiveInstrumentation{def {unknown/Runner} 1 procs}"
        with self.assertRaisesRegex(owner.OwnershipError, "instrumentation_identity_changed"):
            value.finish_after_quit(SID)
        self.assertEqual(client.stops, [])

    def test_same_instrument_instance_finished_transition_waits_for_zero(self):
        value, client = captured(); client.serial = "new-endpoint"; client.finished_transition = True
        value.finish_after_quit(SID)
        self.assertEqual(len(client.stops), 1)
        self.assertEqual(client.transition_reads, 1)
        self.assertTrue(value.cleanup_receipt["physicalTerminalVerified"])

    def test_finished_transition_does_not_admit_new_instance_or_component(self):
        for raw in ["ActiveInstrumentation{def {" + PACKAGE + ".test/Runner} FINISHED 0 procs}",
                    "ActiveInstrumentation{abc {" + PACKAGE + ".test/OtherRunner} FINISHED 0 procs}"]:
            value, client = captured(); client.serial = "new-endpoint"; client.instrument = raw
            with self.assertRaisesRegex(owner.OwnershipError, "instrumentation_identity_changed"):
                value.finish_after_quit(SID)
            self.assertEqual(client.stops, [])

    def test_same_endpoint_residual_is_bounded_and_never_killed(self):
        value, client = captured()
        with self.assertRaisesRegex(owner.OwnershipError, "terminal_unverified"):
            value.finish_after_quit(SID)
        self.assertEqual(client.now, 12)
        self.assertEqual(client.stops, [])

    def test_normal_cleanup_already_gone_needs_no_signal(self):
        for changed in [False, True]:
            value, client = captured(); client.active = False
            if changed: client.serial = "new-endpoint"
            value.finish_after_quit(SID)
            self.assertEqual(client.stops, [])

    def test_lost_signal_response_is_never_replayed(self):
        value, client = captured(); client.serial = "new-endpoint"; client.stop_lost = True
        with self.assertRaises(TimeoutError): value.finish_after_quit(SID)
        with self.assertRaisesRegex(owner.OwnershipError, "already_attempted"): value.finish_after_quit(SID)
        self.assertEqual(len(client.stops), 1)

    def test_atomic_script_recheck_rejection_is_not_retried(self):
        value, client = captured(); client.serial = "new-endpoint"; client.stop_error = True
        with self.assertRaisesRegex(owner.OwnershipError, "read_failed"): value.finish_after_quit(SID)
        self.assertEqual(len(client.stops), 1)

    def test_owned_forward_must_disappear_even_after_processes_gone(self):
        value, client = captured(); client.active = False; client.keep_forward = True
        with self.assertRaisesRegex(owner.OwnershipError, "terminal_unverified"): value.finish_after_quit(SID)
        self.assertEqual(client.stops, [])

    def test_missing_capture_never_gains_authority_to_stop(self):
        value, client = make(); value.session_id = SID; client.active = True; client.serial = "new-endpoint"
        with self.assertRaisesRegex(owner.OwnershipError, "capture_missing"): value.finish_after_quit(SID)
        self.assertEqual(client.stops, [])

    def test_host_change_prevents_any_phone_commands(self):
        value, client = captured(); value.host_probe = lambda port: {**HOST, "pid": 43}
        before = len(client.calls)
        with self.assertRaisesRegex(owner.OwnershipError, "host_changed"): value.finish_after_quit(SID)
        self.assertEqual(len(client.calls), before)

    def test_uid_or_cmdline_disagreement_is_unknown(self):
        for field in ["bad_uid", "bad_cmdline"]:
            value, client = captured(); client.serial = "new-endpoint"; setattr(client, field, True)
            with self.assertRaises(owner.OwnershipError): value.finish_after_quit(SID)
            self.assertEqual(client.stops, [])

    def test_parser_unknown_empty_and_truncated_are_not_zero(self):
        for text in ["", "Unknown command: instrumentation\n", report()[:-1], report().split("  Total")[0]]:
            with self.assertRaises(owner.OwnershipError): owner.parse_instrumentation(text)
        self.assertEqual(owner.parse_instrumentation(report()), ())
        self.assertEqual(owner.parse_forwards("\n \n"), [])
        with self.assertRaises(owner.OwnershipError): owner.parse_forwards("Unknown command: forward\n")

    def test_pid_and_package_scope_cannot_expand(self):
        record = {"pid": 123, "uid": 10042, "startTicks": 777, "name": PACKAGE}
        for changed in [{"pid": 1}, {"pid": -123}, {"uid": 0}, {"name": PACKAGE + ".test"}]:
            with self.assertRaises(owner.OwnershipError): owner.stop_script({**record, **changed})
        self.assertEqual(owner.start_ticks(stat(123, 777), 123), 777)


if __name__ == "__main__": unittest.main()
