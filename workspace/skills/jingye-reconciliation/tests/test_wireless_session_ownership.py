"""Offline tests of yyb connection boundaries; never import its executable top level."""
from __future__ import annotations

import ast
import contextlib
import io
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "yyb_search.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
SID = "11111111-2222-4333-8444-555555555555"


def load(wireless=True):
    class ConnectionUnavailable(RuntimeError):
        pass
    class RequestError(Exception):
        pass
    class GuardError(RuntimeError):
        pass
    reservation = SimpleNamespace(created=Mock(), uncertain=Mock(), closed=Mock(), host={"pid": 42})
    phone = SimpleNamespace(capture=Mock(), finish_after_quit=Mock())
    options = SimpleNamespace(set_capability=Mock())
    link = SimpleNamespace(is_enabled=Mock(return_value=wireless),
                           ensure_connected=Mock(return_value="198.51.100.10:40000"),
                           ConnectionUnavailable=ConnectionUnavailable)
    scope = {
        "re": re, "wireless_link": link, "driver": None, "wait": None,
        "ADB_SERIAL": "TEST-OLD", "options": options,
        "APPIUM_SERVER_URL": "http://127.0.0.1:4723",
        "APPIUM_PERFORMANCE_SETTINGS": {"waitForIdleTimeout": 1500},
        "webdriver": SimpleNamespace(Remote=Mock()), "time": SimpleNamespace(sleep=Mock()),
        "AppiumClientConfig": Mock(side_effect=lambda **kwargs: kwargs),
        "WebDriverWait": Mock(), "NoSuchElementException": LookupError,
        "StaleElementReferenceException": IndexError,
        "requests": SimpleNamespace(Session=Mock(), RequestException=RequestError),
        "appium_guard": SimpleNamespace(begin=Mock(return_value=reservation), GuardError=GuardError),
        "appium_phone": SimpleNamespace(prepare=Mock(return_value=phone)),
        "_session_creation_outcome_uncertain": False,
        "_session_cleanup_uncertain": False,
    }
    scope["appium_guard"].query_local_host = Mock()
    names = {"_owned_appium_session_active", "_close_owned_appium_session",
             "create_appium_session", "recreate_appium_session"}
    nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in names
             or isinstance(n, ast.ClassDef) and n.name in {"AppiumSessionOwnershipError", "SessionCleanupUncertain"}]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope


def driver():
    return SimpleNamespace(session_id=SID, update_settings=Mock(),
                           get_settings=Mock(return_value={"shutdownOnPowerDisconnect": False}),
                           get_window_size=Mock(return_value={"width": 100, "height": 200}), quit=Mock(),
                           _jingye_phone_ownership=SimpleNamespace(finish_after_quit=Mock()),
                           _jingye_guard_reservation=SimpleNamespace(created=Mock(), uncertain=Mock(), closed=Mock()))


def response(status, value):
    return SimpleNamespace(status_code=status, json=lambda: {"value": value})


class WirelessOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def test_verified_endpoint_and_settings_applied_before_first_remote(self):
        scope = load()
        candidate = driver()
        events = []
        scope["wireless_link"].ensure_connected.side_effect = lambda: events.append("connected") or "198.51.100.10:40000"
        scope["webdriver"].Remote.side_effect = lambda *a, **k: events.append("remote") or candidate
        self.assertIs(scope["create_appium_session"]("test"), candidate)
        self.assertEqual(events, ["connected", "remote"])
        self.assertEqual(scope["ADB_SERIAL"], "198.51.100.10:40000")
        scope["options"].set_capability.assert_any_call("appium:suppressKillServer", True)
        scope["options"].set_capability.assert_any_call("appium:newCommandTimeout", 0)
        scope["options"].set_capability.assert_any_call("appium:ignoreHiddenApiPolicyError", True)
        scope["appium_guard"].begin.return_value.created.assert_called_once_with(SID)
        self.assertFalse(candidate.update_settings.call_args.args[0]["shutdownOnPowerDisconnect"])
        self.assertEqual(scope["AppiumClientConfig"].call_args.kwargs["timeout"], 260)
        scope["appium_phone"].prepare.return_value.capture.assert_called_once_with(SID)

    def test_prepare_failure_records_no_remote_and_never_retries(self):
        scope = load()
        scope["appium_phone"].prepare.side_effect = RuntimeError("phone not idle")
        with self.assertRaisesRegex(scope["AppiumSessionOwnershipError"], "creation_intent_no_remote"):
            scope["create_appium_session"]("test")
        scope["webdriver"].Remote.assert_not_called()
        self.assertFalse(scope["_session_creation_outcome_uncertain"])
        self.assertTrue(scope["_session_cleanup_uncertain"])
        scope["appium_guard"].begin.return_value.uncertain.assert_called_once_with("cleanup_uncertain")

    def test_capture_precedes_settings_and_failure_cannot_skip_owned_cleanup(self):
        scope = load(); candidate = driver(); events = []
        scope["webdriver"].Remote.return_value = candidate
        phone = scope["appium_phone"].prepare.return_value
        phone.capture.side_effect = lambda sid: events.append("capture")
        candidate.update_settings.side_effect = lambda settings: events.append("settings")
        scope["create_appium_session"]("test")
        self.assertEqual(events, ["capture", "settings"])

    def test_phone_terminal_failure_never_releases_guard_or_recreates(self):
        scope = load(); old = driver(); scope["driver"] = old
        old._jingye_phone_ownership.finish_after_quit.side_effect = RuntimeError("physical cleanup unknown")
        scope["requests"].Session.return_value = SimpleNamespace(
            get=Mock(return_value=response(404, {"error": "invalid session id"})), close=Mock())
        with self.assertRaises(scope["SessionCleanupUncertain"]):
            scope["recreate_appium_session"]("test")
        old.quit.assert_called_once()
        old._jingye_guard_reservation.closed.assert_not_called()
        old._jingye_guard_reservation.uncertain.assert_called_once_with("cleanup_uncertain")
        scope["webdriver"].Remote.assert_not_called()

    def test_offline_wait_budget_failure_never_sends_remote(self):
        scope = load()
        scope["wireless_link"].ensure_connected.side_effect = scope["wireless_link"].ConnectionUnavailable()
        with self.assertRaises(scope["wireless_link"].ConnectionUnavailable):
            scope["create_appium_session"]("test")
        scope["webdriver"].Remote.assert_not_called()
        scope["time"].sleep.assert_not_called()

    def test_uncertain_remote_creation_is_not_automatically_repeated(self):
        scope = load()
        scope["webdriver"].Remote.side_effect = RuntimeError("response lost")
        with self.assertRaises(scope["AppiumSessionOwnershipError"]):
            scope["create_appium_session"]("test")
        self.assertEqual(scope["webdriver"].Remote.call_count, 1)
        self.assertTrue(scope["_session_creation_outcome_uncertain"])
        scope["appium_guard"].begin.return_value.uncertain.assert_called_once_with("creation_unknown")

    def test_failed_quit_with_exact_invalid_session_still_requires_cleanup_confirmation(self):
        scope = load()
        old = driver()
        old.quit.side_effect = RuntimeError("response lost")
        http = SimpleNamespace(get=Mock(return_value=response(404, {"error": "invalid session id"})),
                               delete=Mock(), close=Mock())
        scope["requests"].Session.return_value = http
        with self.assertRaises(scope["SessionCleanupUncertain"]):
            scope["_close_owned_appium_session"](old)
        self.assertTrue(http.get.call_args.args[0].endswith("/session/" + SID + "/timeouts"))
        http.delete.assert_not_called()
        self.assertTrue(scope["_session_cleanup_uncertain"])
        old._jingye_guard_reservation.uncertain.assert_called_once_with("cleanup_uncertain")

    def test_generic_404_never_permits_replacement(self):
        scope = load()
        old = driver()
        old.quit.side_effect = RuntimeError("response lost")
        http = SimpleNamespace(get=Mock(return_value=response(404, {"error": "unknown command"})),
                               delete=Mock(), close=Mock())
        scope["requests"].Session.return_value = http
        scope["driver"] = old
        with self.assertRaises(scope["AppiumSessionOwnershipError"]):
            scope["recreate_appium_session"]("test")
        scope["webdriver"].Remote.assert_not_called()
        http.delete.assert_not_called()
        self.assertIs(scope["driver"], old)

    def test_uncertain_cleanup_is_not_repeated_by_another_account(self):
        scope = load()
        old = driver()
        old.quit.side_effect = RuntimeError("response lost")
        http = SimpleNamespace(get=Mock(return_value=response(200, {"implicit": 0})),
                               delete=Mock(), close=Mock())
        scope["requests"].Session.return_value = http
        scope["driver"] = old
        for _ in range(2):
            with self.assertRaises(scope["AppiumSessionOwnershipError"]):
                scope["recreate_appium_session"]("test")
        old.quit.assert_called_once()
        http.delete.assert_not_called()
        scope["webdriver"].Remote.assert_not_called()

    def test_still_active_after_failed_quit_does_not_create_another_session(self):
        scope = load()
        old = driver()
        old.quit.side_effect = RuntimeError("response lost")
        http = SimpleNamespace(get=Mock(return_value=response(200, {"implicit": 0})),
                               delete=Mock(), close=Mock())
        scope["requests"].Session.return_value = http
        scope["driver"] = old
        with self.assertRaises(scope["AppiumSessionOwnershipError"]):
            scope["recreate_appium_session"]("test")
        scope["webdriver"].Remote.assert_not_called()

    def test_usb_keeps_original_remote_and_optional_settings_behavior(self):
        scope = load(wireless=False)
        candidate = driver()
        candidate.update_settings.side_effect = RuntimeError("optional setting unsupported")
        scope["webdriver"].Remote.side_effect = [RuntimeError("pre-existing USB retry"), candidate]
        self.assertIs(scope["create_appium_session"]("test"), candidate)
        self.assertEqual(scope["webdriver"].Remote.call_count, 2)
        scope["wireless_link"].ensure_connected.assert_not_called()
        scope["AppiumClientConfig"].assert_not_called()
        scope["options"].set_capability.assert_not_called()
        candidate.get_settings.assert_not_called()
        scope["appium_guard"].begin.assert_not_called()

    def test_durable_guard_refusal_sends_no_remote(self):
        scope = load()
        scope["appium_guard"].begin.side_effect = scope["appium_guard"].GuardError("same_host_unresolved")
        with self.assertRaises(scope["AppiumSessionOwnershipError"]):
            scope["create_appium_session"]("test")
        scope["webdriver"].Remote.assert_not_called()

    def test_normal_quit_must_also_clear_own_durable_record(self):
        scope = load()
        old = driver()
        scope["_close_owned_appium_session"](old)
        old.quit.assert_called_once()
        old._jingye_guard_reservation.closed.assert_called_once()
        old._jingye_guard_reservation.uncertain.assert_not_called()

    def test_guard_clear_failure_blocks_recreate_even_if_quit_returned(self):
        scope = load()
        old = driver()
        old._jingye_guard_reservation.closed.side_effect = scope["appium_guard"].GuardError("write_failed")
        scope["driver"] = old
        scope["requests"].Session.return_value = SimpleNamespace(
            get=Mock(return_value=response(404, {"error": "invalid session id"})), close=Mock())
        with self.assertRaises(scope["SessionCleanupUncertain"]):
            scope["recreate_appium_session"]("test")
        scope["webdriver"].Remote.assert_not_called()
        old._jingye_guard_reservation.uncertain.assert_called_once_with("cleanup_uncertain")


if __name__ == "__main__":
    unittest.main()
