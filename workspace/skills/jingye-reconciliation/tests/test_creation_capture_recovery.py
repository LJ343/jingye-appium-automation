"""Actual candidate ownership + Y boundaries; all phone/HTTP effects are fake."""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import importlib.util

import test_phone_process_ownership as base
import test_wireless_session_ownership as boundary

owner = base.owner
DSID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
NET_HEAD = "sl local_address remote_address st tx_queue rx_queue tr tm->when retrnsmt uid timeout inode\n"


def sessions(token):
    # Official GetSessions uses wrapper NO_ID, not the downstream session ID.
    return {"sessionId": "NO_ID", "value": [{"id": DSID, "sessionId": DSID,
            "capabilities": {owner.OWNERSHIP_CAPABILITY: token}}]}


class CreationClient(base.FakeClient):
    def __init__(self):
        super().__init__(); self.entries = []; self.next_port = 49100; self.removes = []
        self.inode = 9001; self.socket_uid = self.uid; self.fd_reads = 0; self.fault = None
        self.fault_done = False; self.unknown_allocation = False; self.http_calls = 0
        self.base_entries = None
    def _exec(self, args, timeout, deadline):
        tail = args[2:] if args[:1] == ["-s"] else args
        if tail == ["shell", "ps", "-A", "-o", "PID,UID,NAME"] and self.active and self.fault == "snapshot" and not self.fault_done:
            self.fault_done = True; self.serial = "new-endpoint"; raise TimeoutError("synthetic read loss")
        if tail == ["forward", "--no-rebind", "tcp:0", "tcp:6790"]:
            self.next_port += 1; self.entries.append((self.serial, "tcp:" + str(self.next_port), "tcp:6790"))
            if self.unknown_allocation: raise TimeoutError("synthetic allocation response loss")
            return SimpleNamespace(returncode=0, stdout=str(self.next_port) + "\n")
        if tail == ["forward", "--list"]:
            old = [("old-endpoint", "tcp:8200", "tcp:6790")] if (self.active and self.serial == "old-endpoint") or self.keep_forward else []
            if self.base_entries is not None: old = self.base_entries if self.active or self.keep_forward else []
            return SimpleNamespace(returncode=0, stdout="".join(" ".join(r) + "\n" for r in old + self.entries))
        if tail[:2] == ["forward", "--remove"]:
            self.removes.append(tail[2]); self.entries = [r for r in self.entries if r[1] != tail[2]]
            return SimpleNamespace(returncode=0, stdout="")
        if tail[:3] == ["shell", "cat", "/proc/net/tcp"]:
            return SimpleNamespace(returncode=0, stdout=NET_HEAD)
        if tail[:3] == ["shell", "cat", "/proc/net/tcp6"]:
            row = f"0: {'0'*32}:1A86 {'0'*32}:0000 0A 00000000:00000000 00:00000000 00000000 {self.socket_uid} 0 {self.inode} 1\n"
            return SimpleNamespace(returncode=0, stdout=NET_HEAD + row)
        if tail[:5] == ["shell", "run-as", base.PACKAGE, "ls", "-l"]:
            self.fd_reads += 1
            if self.fault == "proof_fd" and self.fd_reads == 2 and not self.fault_done:
                self.fault_done = True; self.serial = "newer-endpoint"; self.entries = []
                raise TimeoutError("synthetic second read loss")
            return SimpleNamespace(returncode=0, stdout=f"lrwx------ 1 u0_a42 u0_a42 64 2026-11-04 16:20 50 -> socket:[{self.inode}]\n")
        return super()._exec(args, timeout, deadline)


def make():
    client = CreationClient()
    value = owner.prepare(SimpleNamespace(_default_client=lambda: client), "http://127.0.0.1:4723", base.HOST,
                          host_probe=lambda port: base.HOST)
    return value, client


class CreationCaptureTests(unittest.TestCase):
    def test_token_is_only_exact_real_session_field_and_no_id_wrapper_is_valid(self):
        self.assertEqual(owner.session_token_identity(sessions("nonce"), "nonce"), DSID)
        for change in [lambda v: v["value"][0]["capabilities"].clear(),
                       lambda v: v["value"][0]["capabilities"].update({owner.OWNERSHIP_CAPABILITY: {"token": "nonce"}}),
                       lambda v: v["value"][0].update(sessionId=base.SID),
                       lambda v: v["value"].append(copy.deepcopy(v["value"][0])),
                       lambda v: v.update(error="failure"),
                       lambda v: v["value"][0].update(error="failure")]:
            value = sessions("nonce"); change(value)
            with self.assertRaises(owner.OwnershipError): owner.session_token_identity(value, "nonce")
        nested = sessions("nonce"); nested["value"][0]["capabilities"] = {"desired": {owner.OWNERSHIP_CAPABILITY: "nonce"}}
        with self.assertRaises(owner.OwnershipError): owner.session_token_identity(nested, "nonce")

    def test_pre_capture_change_with_empty_forward_proves_then_requires_quit(self):
        value, client = make(); client.active = True; client.serial = "new-endpoint"
        with patch.object(owner, "read_sessions_json", return_value=sessions(value.creation_token)):
            with self.assertRaises(owner.CreationTransportChanged): value.capture(base.SID)
        self.assertEqual(value.creation_transport, "old-endpoint")
        self.assertEqual(value.capture_transport, "new-endpoint")
        self.assertEqual(value.owned["forwards"], [])
        self.assertEqual(client.stops, [])
        self.assertTrue(all(r["created"] and r["removed"] for r in value.proof_forwards))
        value.finish_after_quit(base.SID)
        self.assertEqual(len(client.stops), 1)
        self.assertTrue(value.cleanup_receipt["physicalTerminalVerified"])
        with self.assertRaises(AttributeError): value.creation_transport = "replace"

    def test_missing_or_wrong_token_never_promotes_or_signals(self):
        for response in [sessions("wrong"), {"value": []}, {"error": "failure", "value": []}]:
            value, client = make(); client.active = True; client.serial = "new-endpoint"
            with patch.object(owner, "read_sessions_json", return_value=response):
                with self.assertRaises(owner.OwnershipError): value.capture(base.SID)
            self.assertIsNone(value.owned)
            with self.assertRaisesRegex(owner.OwnershipError, "capture_missing"): value.finish_after_quit(base.SID)
            self.assertEqual(client.stops, [])
            self.assertEqual(client.entries, [])

    def test_socket_uid_or_pid_inode_mismatch_never_promotes(self):
        value, client = make(); client.active = True; client.serial = "new-endpoint"; client.socket_uid += 1
        with patch.object(owner, "read_sessions_json", return_value=sessions(value.creation_token)) as http:
            with self.assertRaisesRegex(owner.OwnershipError, "socket_ownership_unverified"): value.capture(base.SID)
            http.assert_not_called()
        self.assertIsNone(value.owned); self.assertEqual(client.stops, [])
        value, client = make(); client.active = True; client.serial = "new-endpoint"
        real = client._exec
        def changed_fd(args, timeout, deadline):
            result = real(args, timeout, deadline)
            if "ls" in args: result.stdout = result.stdout.replace("9001", "9999")
            return result
        client._exec = changed_fd
        with self.assertRaisesRegex(owner.OwnershipError, "socket_pid_unverified"): value.capture(base.SID)
        self.assertIsNone(value.owned); self.assertEqual(client.stops, [])

    def test_capture_read_loss_restarts_whole_proof_with_same_total_deadline(self):
        for fault in ("snapshot", "proof_fd"):
            value, client = make(); client.active = True; client.fault = fault
            if fault == "proof_fd": client.serial = "new-endpoint"
            with patch.object(owner, "read_sessions_json", return_value=sessions(value.creation_token)):
                with self.assertRaises(owner.CreationTransportChanged): value.capture(base.SID)
            self.assertEqual(value.capture_read_retries, 1)
            self.assertTrue(value.capture_diagnostic["authoritative"])
            self.assertTrue(all(r["created"] and r["removed"] for r in value.proof_forwards))
            self.assertEqual(client.entries, [])
            self.assertEqual(client.stops, [])

    def test_mid_http_transport_loss_cleans_own_forward_before_whole_proof_retry(self):
        value, client = make(); client.active = True; client.serial = "new-endpoint"
        calls = []
        def interrupted(*args):
            calls.append(1)
            if len(calls) == 1: raise owner.CreationReadTransportError()
            return sessions(value.creation_token)
        with patch.object(owner, "read_sessions_json", side_effect=interrupted):
            with self.assertRaises(owner.CreationTransportChanged): value.capture(base.SID)
        self.assertEqual(value.capture_read_retries, 1)
        self.assertEqual(len(value.proof_forwards), 2)
        self.assertTrue(all(r["created"] and r["removed"] for r in value.proof_forwards))
        self.assertEqual(client.removes, ["tcp:49101", "tcp:49102"])
        self.assertEqual(client.entries, []); self.assertEqual(client.stops, [])

    def test_allocation_response_loss_is_not_replayed_or_guessed_removed(self):
        value, client = make(); client.active = True; client.serial = "new-endpoint"; client.unknown_allocation = True
        with self.assertRaises(TimeoutError): value.capture(base.SID)
        self.assertIsNone(value.owned)
        self.assertEqual(len(client.entries), 1); self.assertEqual(client.removes, [])
        self.assertEqual(len(value.proof_forwards), 1); self.assertEqual(client.stops, [])

    def test_pid_or_instrument_change_during_proof_is_not_read_retry(self):
        for changed in ("ticks", "instrument"):
            value, client = make(); client.active = True; client.serial = "new-endpoint"
            def response(*args):
                if changed == "ticks": client.ticks += 1
                else: client.instrument = "ActiveInstrumentation{def {" + base.PACKAGE + ".test/Runner} 1 procs}"
                return sessions(value.creation_token)
            with patch.object(owner, "read_sessions_json", side_effect=response):
                with self.assertRaisesRegex(owner.OwnershipError, "snapshot_changed"): value.capture(base.SID)
            self.assertIsNone(value.owned); self.assertEqual(value.capture_read_retries, 0)
            self.assertEqual(client.entries, []); self.assertEqual(client.stops, [])

    def test_read_retry_has_one_bounded_deadline_not_infinite_wait(self):
        value, client = make(); client.active = True
        real = client._exec
        def failed(args, timeout, deadline):
            if "ps" in args:
                client.now += min(80, deadline - client.now)
                raise TimeoutError("synthetic long transport failure")
            return real(args, timeout, deadline)
        client._exec = failed
        with self.assertRaisesRegex(owner.OwnershipError, "budget_exhausted"): value.capture(base.SID)
        self.assertEqual(client.now, 300); self.assertIsNone(value.owned)
        self.assertEqual(client.stops, [])

    def test_prepare_endpoint_is_applied_to_actual_remote_options(self):
        scope = boundary.load(); scope["appium_phone"].prepare.return_value.creation_transport = "198.51.100.10:45555"
        candidate = boundary.driver(); scope["webdriver"].Remote.return_value = candidate
        with redirect_stdout(io.StringIO()): scope["create_appium_session"]("offline prepare change")
        caps = {call.args[0]: call.args[1] for call in scope["options"].set_capability.call_args_list}
        self.assertEqual(caps["appium:udid"], "198.51.100.10:45555")
        self.assertEqual(caps["appium:deviceName"], "198.51.100.10:45555")
        self.assertEqual(caps[owner.OWNERSHIP_CAPABILITY], "offline-token")

    def integrated(self, unknown=False):
        path = Path(base.SOURCE).with_name("jingye_appium_guard.py")
        spec = importlib.util.spec_from_file_location("creation_guard", path)
        guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)
        client = CreationClient(); scope = boundary.load(); owners = []; events = []
        scope["wireless_link"]._default_client = lambda: client
        scope["wireless_link"].ensure_connected.side_effect = lambda: client._ensure(300)
        def prepare(*args, **kwargs):
            result = owner.prepare(*args, **kwargs); owners.append(result); return result
        scope["appium_phone"] = SimpleNamespace(prepare=prepare, OWNERSHIP_CAPABILITY=owner.OWNERSHIP_CAPABILITY)
        with tempfile.TemporaryDirectory() as folder:
            journal = Path(folder) / "synthetic-guard.json"
            def begin(url):
                ticket = guard.SessionGuard(journal, url, host_probe=lambda port: base.HOST).begin()
                closed = ticket.closed
                def close():
                    closed(); events.append("guard_closed")
                ticket.closed = close
                return ticket
            scope["appium_guard"] = SimpleNamespace(begin=begin, GuardError=guard.GuardError,
                                                      query_local_host=lambda port: base.HOST)
            one, two = boundary.driver(), boundary.driver()
            two.session_id = "22222222-3333-4444-8555-666666666666"
            one.quit.side_effect = lambda: events.append("quit_one_HTTP200")
            one.update_settings.side_effect = lambda _: events.append("settings_one")
            two.update_settings.side_effect = lambda _: events.append("settings_two")
            def remote(*args, **kwargs):
                count = scope["webdriver"].Remote.call_count
                events.append("remote_" + str(count))
                client.active = True
                if count == 1:
                    client.serial = "new-endpoint"; client.base_entries = []
                    return one
                self.assertIn("guard_closed", events)
                self.assertFalse(json.loads(journal.read_bytes())["lastClosed"]["state"] != "closed")
                client.pid += 1; client.ticks += 1
                client.instrument = "ActiveInstrumentation{def {" + base.PACKAGE + ".test/Runner} 1 procs}"
                client.base_entries = [(client.serial, "tcp:8200", "tcp:6790")]
                return two
            scope["webdriver"].Remote.side_effect = remote
            scope["requests"].Session.return_value = SimpleNamespace(
                get=Mock(return_value=boundary.response(404, {"error": "invalid session id"})), close=Mock())
            real_exec = client._exec
            def track(args, timeout, deadline):
                if "sh" in args and "-c" in args: events.append("TERM_one")
                return real_exec(args, timeout, deadline)
            client._exec = track
            def server(*args): return sessions("wrong" if unknown else owners[-1].creation_token)
            with patch.object(owner, "read_sessions_json", side_effect=server), redirect_stdout(io.StringIO()):
                if unknown:
                    with self.assertRaises(scope["SessionCleanupUncertain"]): scope["create_appium_session"]("unknown capture")
                else:
                    self.assertIs(scope["create_appium_session"]("pre-capture transport"), two)
            record = json.loads(journal.read_bytes())["record"]
            if unknown:
                self.assertEqual(record["state"], "cleanup_uncertain")
                self.assertEqual(scope["webdriver"].Remote.call_count, 1)
                self.assertEqual(client.stops, [])
                self.assertNotIn("guard_closed", events)
            else:
                self.assertEqual(record["state"], "active")
                self.assertEqual(record["sessionId"], two.session_id)
                self.assertEqual(scope["webdriver"].Remote.call_count, 2)
                self.assertEqual(len(client.stops), 1)
                self.assertLess(events.index("quit_one_HTTP200"), events.index("TERM_one"))
                self.assertLess(events.index("TERM_one"), events.index("guard_closed"))
                self.assertLess(events.index("guard_closed"), events.index("remote_2"))
                self.assertIn("settings_two", events)
            self.assertNotIn("settings_one", events)
            self.assertFalse(scope["_session_creation_outcome_uncertain"])
            self.assertEqual(client.entries, [])

    def test_integrated_proved_creation_closes_before_second_remote(self): self.integrated()
    def test_integrated_unknown_token_stays_blocked_without_second_remote(self): self.integrated(unknown=True)

    def test_nontransport_read_rejection_is_not_retried(self):
        value, client = make(); client.active = True
        real = client._exec
        def rejected(args, timeout, deadline):
            if "ps" in args: return SimpleNamespace(returncode=1, stdout="", stderr="Permission denied")
            return real(args, timeout, deadline)
        client._exec = rejected
        with self.assertRaisesRegex(owner.OwnershipError, "phone_owner_read_failed"): value.capture(base.SID)
        self.assertEqual(value.capture_read_retries, 0)
        self.assertIsNone(value.owned)

    def test_http_reader_is_bounded_and_rejects_errors_or_ambiguous_lengths(self):
        class Stream:
            def __init__(self, chunks): self.chunks = iter(chunks); self.sent = []; self.timeouts = []
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): self.timeouts.append(value)
            def sendall(self, value): self.sent.append(value)
            def recv(self, limit): return next(self.chunks, b"")
        body = json.dumps(sessions("nonce")).encode()
        good = b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        stream = Stream([good])
        with patch.object(owner.socket, "create_connection", return_value=stream):
            self.assertEqual(owner.read_sessions_json(49123, 10, lambda: 0), sessions("nonce"))
        self.assertTrue(stream.sent[0].startswith(b"GET /sessions HTTP/1.1\r\n"))
        for raw in [good.replace(b"200 OK", b"404 Not Found"), good.replace(b"Content-Length:", b"Transfer-Encoding:"),
                    good.replace(b"\r\n\r\n", b"\r\nContent-Length: 1\r\n\r\n", 1),
                    good.replace(str(len(body)).encode() + b"\r\n", b"1048577\r\n", 1)]:
            with patch.object(owner.socket, "create_connection", return_value=Stream([raw])):
                with self.assertRaises(owner.OwnershipError): owner.read_sessions_json(49123, 10, lambda: 0)

    def test_http_trickle_cannot_reset_the_total_deadline(self):
        client = CreationClient()
        class SlowStream:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): self.timeout = value
            def sendall(self, value): pass
            def recv(self, limit): client.now += 3; return b"H"
        with patch.object(owner.socket, "create_connection", return_value=SlowStream()):
            with self.assertRaisesRegex(owner.OwnershipError, "budget_exhausted"):
                owner.read_sessions_json(49123, 10, client.clock)
        self.assertLessEqual(client.now, 12)

    def test_http_disconnect_is_retryable_but_invalid_json_is_not(self):
        class Stream:
            def __init__(self, broken): self.broken = broken
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def settimeout(self, value): pass
            def sendall(self, value): pass
            def recv(self, limit):
                if self.broken: raise ConnectionResetError("synthetic loss")
                return b"HTTP/1.1 200 OK\r\nContent-Length: 1\r\n\r\nx"
        with patch.object(owner.socket, "create_connection", return_value=Stream(True)):
            with self.assertRaises(owner.CreationReadTransportError): owner.read_sessions_json(49123, 10, lambda: 0)
        with patch.object(owner.socket, "create_connection", return_value=Stream(False)):
            try: owner.read_sessions_json(49123, 10, lambda: 0)
            except owner.OwnershipError as error:
                self.assertNotIsInstance(error, owner.CreationReadTransportError)
            else: self.fail("invalid JSON accepted")


if __name__ == "__main__": unittest.main()
