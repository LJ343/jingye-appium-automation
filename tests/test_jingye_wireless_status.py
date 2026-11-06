"""Offline checks only. No real device, ADB daemon, or business calls."""
import hashlib
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "wireless_status", Path(__file__).resolve().parents[1] / "operations" / "jingye_wireless_status.py")
status = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(status)
IP = "192.0.2.5"
DIGEST = hashlib.sha256(b"synthetic-phone").hexdigest()


class FakeClient:
    def __init__(self, rows=(), replies=None, error=None):
        self.rows, self.replies, self.error = rows, replies or {}, error
        self.queried = []

    def devices(self):
        if self.error:
            raise self.error
        return self.rows

    def identity(self, endpoint, deadline):
        self.queried.append(endpoint)
        response = self.replies.get(endpoint, TimeoutError())
        if isinstance(response, Exception):
            raise response
        return response


class Wire:
    def __init__(self, response):
        self.response, self.sent, self.closed = response, [], False

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.closed = True

    def settimeout(self, seconds):
        if seconds <= 0:
            raise AssertionError("unbounded timeout")

    def recv(self, count):
        # Fragment every reply down to one byte to exercise framing.
        result, self.response = self.response[:min(1, count)], self.response[min(1, count):]
        return result

    def sendall(self, data):
        self.sent.append(data)


class StatusTests(unittest.TestCase):
    def test_current_dynamic_port_and_identity_required(self):
        client = FakeClient([(IP + ":47777", "device")], {IP + ":47777": DIGEST})
        self.assertEqual(status.check_connection(IP, DIGEST, client)[0], "CONNECTED")
        self.assertEqual(client.queried, [IP + ":47777"])

    def test_device_listing_alone_is_not_success(self):
        client = FakeClient([(IP + ":47777", "device")])
        self.assertEqual(status.check_connection(IP, DIGEST, client)[0], "UNRESPONSIVE")

    def test_wrong_identity_is_not_success(self):
        client = FakeClient([(IP + ":47777", "device")], {IP + ":47777": "0" * 64})
        self.assertEqual(status.check_connection(IP, DIGEST, client)[0], "IDENTITY_MISMATCH")

    def test_stale_endpoint_does_not_hide_new_endpoint(self):
        client = FakeClient([(IP + ":47000", "device"), (IP + ":48000", "device")],
                            {IP + ":48000": DIGEST})
        self.assertEqual(status.check_connection(IP, DIGEST, client)[0], "CONNECTED")

    def test_usb_and_other_phone_are_not_probed_or_reported_online(self):
        client = FakeClient([("usb-synthetic", "device"), ("192.0.2.6:48000", "device")])
        self.assertEqual(status.check_connection(IP, DIGEST, client)[0], "DISCONNECTED")
        self.assertEqual(client.queried, [])

    def test_offline_unauthorized_and_absent_are_distinct(self):
        for state, expected in [("offline", "OFFLINE"), ("unauthorized", "UNAUTHORIZED"),
                                ("recovery", "UNKNOWN")]:
            with self.subTest(state=state):
                client = FakeClient([(IP + ":48000", state)])
                self.assertEqual(status.check_connection(IP, DIGEST, client)[0], expected)
                self.assertEqual(client.queried, [])
        self.assertEqual(status.check_connection(IP, DIGEST, FakeClient())[0], "DISCONNECTED")

    def test_absent_daemon_and_broken_protocol_are_not_disconnected_phone(self):
        for error, expected in [(ConnectionRefusedError(), "ADB_UNAVAILABLE"),
                                (status.ProbeError(), "UNKNOWN")]:
            with self.subTest(expected=expected):
                self.assertEqual(status.check_connection(IP, DIGEST, FakeClient(error=error))[0], expected)

    def test_unscoped_mdns_is_unknown_and_never_queried(self):
        client = FakeClient([("adb-synthetic._adb-tls-connect._tcp", "device")])
        self.assertEqual(status.check_connection(IP, DIGEST, client)[0], "UNKNOWN")
        self.assertEqual(client.queried, [])

    def test_malformed_or_out_of_range_endpoint_never_selected(self):
        for value in [IP + ":65536", IP + ":0", IP + ":04800", IP + ":48000;bad", IP]:
            self.assertFalse(status.in_scope(value, IP))

    def test_protocol_list_framing_and_no_management_commands(self):
        payload = (IP + ":48000\tdevice product:synthetic\n").encode()
        wire = Wire(b"OKAY" + f"{len(payload):04x}".encode() + payload)
        calls = []
        def connect(address, timeout):
            calls.append(address)
            return wire
        client = status.ExistingADB(connect)
        self.assertEqual(client.devices(), [(IP + ":48000", "device")])
        self.assertEqual(calls, [("127.0.0.1", 5037)])
        self.assertEqual(wire.sent, [b"000ehost:devices-l"])
        self.assertTrue(wire.closed)

    def test_identity_protocol_only_uses_fixed_readonly_command(self):
        wire = Wire(b"OKAYOKAYsynthetic-phone\r\n")
        client = status.ExistingADB(lambda address, timeout: wire)
        self.assertEqual(client.identity(IP + ":48000", time.monotonic() + 1), DIGEST)
        services = [data[4:] for data in wire.sent]
        self.assertEqual(services, [("host:transport:" + IP + ":48000").encode(),
                                    b"shell:getprop ro.serialno"])

    def test_invalid_truncated_and_failed_protocol_replies_are_rejected(self):
        for response in [b"OKAYxxxx", b"OKAY0004ab", b"FAILprivate-reply"]:
            with self.subTest(response=response[:4]):
                wire = Wire(response)
                with self.assertRaises(status.ProbeError):
                    status.ExistingADB(lambda address, timeout: wire).devices()

    def test_phone_output_is_bounded_and_empty_or_multiline_is_rejected(self):
        for response in [b"", b"a\nb", b"x" * 513]:
            with self.subTest(length=len(response)):
                wire = Wire(b"OKAYOKAY" + response)
                with self.assertRaises(status.ProbeError):
                    status.ExistingADB(lambda address, timeout: wire).identity(
                        IP + ":48000", time.monotonic() + 1)

    def test_missing_and_invalid_config_fail_without_network(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "synthetic.json"
            with self.assertRaises(OSError):
                status.read_config(path)
            path.write_text(json.dumps({"expected_identity_sha256": DIGEST,
                                        "discovery": {"allowed_ipv4": [IP]}}), encoding="utf-8")
            self.assertEqual(status.read_config(path), (IP, DIGEST))
            for value in [{}, [], {"expected_identity_sha256": "secret-invalid"},
                          {"expected_identity_sha256": DIGEST,
                           "discovery": {"allowed_ipv4": [IP, "192.0.2.6"]}}]:
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises((status.ProbeError, ValueError)):
                    status.read_config(path)

    def test_watch_ctrl_c_exits_only_view_and_prints_sanitized_snapshot(self):
        value = {"observed_at": "2026-11-05T12:00:00+08:00", "code": "OFFLINE",
                 "message": status.MESSAGES["OFFLINE"], "phone_response_ms": None,
                 "check_elapsed_ms": 1}
        output = io.StringIO()
        with patch.object(status.sys, "argv", ["status", "--watch"]), \
                patch.object(status, "sample", return_value=value) as sample, \
                patch.object(status.time, "sleep", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(output):
            self.assertEqual(status.main(), 0)
        sample.assert_called_once()
        self.assertIn("OFFLINE", output.getvalue())
        self.assertIn("已退出查看", output.getvalue())

    def test_config_failure_does_not_touch_adb_or_expose_exception(self):
        with patch.object(status, "read_config", side_effect=ValueError("private-value")), \
                patch.object(status, "ExistingADB") as client:
            value = status.sample()
        client.assert_not_called()
        self.assertEqual(value["code"], "CONFIG_UNAVAILABLE")
        self.assertNotIn("private-value", json.dumps(value))


if __name__ == "__main__":
    unittest.main()
