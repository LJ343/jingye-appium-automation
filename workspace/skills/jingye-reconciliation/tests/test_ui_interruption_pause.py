"""Offline AST tests only: never import yyb_search's executable business top level."""
import ast
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException, StaleElementReferenceException, WebDriverException)
from selenium.webdriver.support import expected_conditions as EC


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "yyb_search.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
NAMES = {"ensure_native_context", "_ui_allowed_packages", "_ui_pause_if_external",
         "_ui_readonly_wait", "wait_visible", "wait_clickable", "wait_nonempty_text",
         "wait_for_any_visible", "_ui_wireless_click_step", "click_step",
         "get_visible_freight_snapshots", "query_current_vehicle"}


class FakeClock:
    def __init__(self):
        self.value = 0
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += seconds


class Element:
    text = "synthetic"

    def __init__(self):
        self.click = Mock()

    def is_displayed(self):
        return True

    def is_enabled(self):
        return True


class Driver:
    current_context = "NATIVE_APP"

    def __init__(self):
        self.packages = ["com.ibb.tizi"]
        self.package_calls = 0
        self.results = []
        self.source_calls = 0
        self.switch_to = SimpleNamespace(context=Mock())

    @property
    def current_package(self):
        self.package_calls += 1
        value = self.packages.pop(0) if len(self.packages) > 1 else self.packages[0]
        if isinstance(value, Exception):
            raise value
        return value

    @property
    def page_source(self):
        self.source_calls += 1
        return "synthetic-rebound-source"

    def find_element(self, *args):
        value = self.results.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def find_elements(self, *args):
        return self.find_element(*args)


def load(wireless=True):
    driver, clock = Driver(), FakeClock()
    class Wait:
        def until(self, condition):
            try:
                result = condition(driver)
            except (NoSuchElementException, StaleElementReferenceException):
                result = False
            if not result:
                raise TimeoutException("synthetic wait expired")
            return result
    scope = {"driver": driver, "json": json,
             "wireless_link": SimpleNamespace(is_enabled=lambda: wireless),
             "time": SimpleNamespace(monotonic=clock.now, sleep=clock.sleep),
             "EC": EC, "TimeoutException": TimeoutException,
             "NoSuchElementException": NoSuchElementException,
             "StaleElementReferenceException": StaleElementReferenceException,
             "WebDriverException": WebDriverException,
             "TRANSIENT_UI_ERRORS": (TimeoutException, NoSuchElementException,
                                     StaleElementReferenceException, WebDriverException),
             "AppiumBy": SimpleNamespace(ID="id", XPATH="xpath"),
             "is_appium_session_lost": lambda e: "invalid session id" in str(e),
             "raise_if_appium_session_lost": Mock(), "fast_wait": Mock(side_effect=lambda _: Wait()),
             "AccountSessionSuspectedError": RuntimeError}
    nodes = [n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name in NAMES]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope, driver, clock


class UiInterruptionPauseTest(unittest.TestCase):
    def setUp(self):
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()
        self.addCleanup(self.quiet.__exit__, None, None, None)

    def test_all_public_waits_resume_original_read_condition_after_external_ui(self):
        for name in ("wait_visible", "wait_clickable", "wait_nonempty_text", "wait_for_any_visible"):
            with self.subTest(name=name):
                scope, driver, clock = load()
                element = Element()
                driver.results = [([] if name == "wait_for_any_visible" else NoSuchElementException()),
                                  ([element] if name == "wait_for_any_visible" else element)]
                driver.packages = ["synthetic.external", "synthetic.external", "com.ibb.tizi"]
                locator = ("id", "com.ibb.tizi:id/synthetic")
                result = scope[name]([locator] if name == "wait_for_any_visible" else locator)
                self.assertTrue(result)
                self.assertEqual(clock.sleeps, [2.0, 2.0])
                element.click.assert_not_called()
                self.assertEqual(scope["fast_wait"].call_count, 2)

    def test_successful_wait_has_no_foreground_queries(self):
        scope, driver, clock = load()
        element = Element()
        driver.results = [element]
        self.assertIs(scope["wait_visible"](("id", "com.ibb.tizi:id/synthetic")), element)
        self.assertEqual(driver.package_calls, 0)
        self.assertEqual(clock.sleeps, [])

    def test_usb_timeout_has_no_pause_or_package_queries(self):
        scope, driver, clock = load(False)
        driver.results = [NoSuchElementException()]
        with self.assertRaises(TimeoutException):
            scope["wait_visible"](("id", "synthetic"))
        self.assertEqual(driver.package_calls, 0)
        self.assertFalse(scope["_ui_pause_if_external"]("test"))
        self.assertEqual(clock.sleeps, [])

    def test_unknown_own_page_is_bounded_and_original_timeout_propagates(self):
        scope, driver, clock = load()
        driver.results = [NoSuchElementException()]
        with self.assertRaises(TimeoutException):
            scope["wait_visible"](("id", "com.ibb.tizi:id/absent"))
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(scope["fast_wait"].call_count, 1)

    def test_probe_error_and_driver_error_propagate_unchanged(self):
        for error in (WebDriverException("synthetic error"), ConnectionError("synthetic offline")):
            scope, driver, _ = load()
            driver.packages = ["synthetic.external", error]
            with self.assertRaises(type(error)) as caught:
                scope["_ui_pause_if_external"]("test")
            self.assertIs(caught.exception, error)
            scope, driver, _ = load()
            driver.results = [error]
            with self.assertRaises(type(error)) as caught:
                scope["wait_for_any_visible"]([("id", "synthetic")])
            self.assertIs(caught.exception, error)

    def test_heartbeat_is_safe_and_pause_does_not_touch_account_retry(self):
        scope, _, clock = load()
        probe = Mock(side_effect=["synthetic.external"] * 32 + ["com.ibb.tizi"])
        events = []
        scope["account_attempt"] = 1
        self.assertTrue(scope["_ui_pause_if_external"]("test", package_probe=probe,
                       clock=clock.now, sleeper=clock.sleep, emit=events.append))
        self.assertEqual(scope["account_attempt"], 1)
        self.assertIn("waiting_external_ui", {e["event"] for e in events})
        self.assertNotIn("synthetic.external", json.dumps(events))

    def test_browser_is_allowed_only_for_explicit_browser_locator_or_phase(self):
        scope, driver, clock = load()
        self.assertEqual(scope["_ui_allowed_packages"]([("id", "com.honor.browser:id/download")]),
                         {"com.honor.browser"})
        self.assertEqual(scope["_ui_allowed_packages"]([("id", "android:id/button1")]),
                         {"com.ibb.tizi"})
        driver.packages = ["com.honor.browser"]
        self.assertFalse(scope["_ui_pause_if_external"]("pdf", {"com.ibb.tizi", "com.honor.browser"}))
        driver.packages = ["com.honor.browser", "com.ibb.tizi"]
        self.assertTrue(scope["_ui_pause_if_external"]("business"))
        self.assertEqual(clock.sleeps, [2.0])

    def test_click_sent_once_then_only_target_wait_resumes(self):
        scope, driver, _ = load()
        element, target = Element(), Element()
        scope["wait_clickable"] = Mock(return_value=element)
        driver.results = [NoSuchElementException(), target]
        driver.packages = ["synthetic.external", "com.ibb.tizi"]
        self.assertTrue(scope["click_step"](("id", "com.ibb.tizi:id/source"), "synthetic",
                        ("id", "com.ibb.tizi:id/target")))
        element.click.assert_called_once_with()
        scope["wait_clickable"].assert_called_once()

    def test_unconfirmed_click_never_replayed_even_if_target_stays_unknown(self):
        scope, _, _ = load()
        element = Element()
        scope["wait_clickable"] = Mock(return_value=element)
        scope["wait_visible"] = Mock(side_effect=TimeoutException("target unknown"))
        with self.assertRaises(TimeoutException):
            scope["click_step"](("id", "source"), "synthetic", ("id", "target"), retries=3)
        element.click.assert_called_once_with()

    def test_click_error_after_external_ui_checks_target_without_reclick(self):
        scope, driver, _ = load()
        element = Element()
        element.click.side_effect = WebDriverException("synthetic uncertain response")
        scope["wait_clickable"] = Mock(return_value=element)
        scope["wait_visible"] = Mock(return_value=Element())
        driver.packages = ["synthetic.external", "com.ibb.tizi"]
        self.assertTrue(scope["click_step"](("id", "source"), "synthetic", ("id", "target")))
        element.click.assert_called_once_with()
        scope["wait_visible"].assert_called_once()

    def test_purchase_empty_branch_reacquires_cards_after_pause(self):
        scope, driver, _ = load()
        function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
                        and n.name == "run_full_flow_for_account")
        node = next(n for n in function.body if isinstance(n, ast.If)
                    and "purchase_result_empty" in ast.unparse(n.test)
                    and "wireless_link" in ast.unparse(n.test))
        driver.results = [[Element()]]
        scope.update(initial_purchase_cards=[], purchase_result_empty=True)
        driver.packages = ["synthetic.external", "com.ibb.tizi"]
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), scope)
        self.assertFalse(scope["purchase_result_empty"])
        self.assertEqual(len(scope["initial_purchase_cards"]), 1)

    def test_both_pagination_branches_discard_old_source_on_resume(self):
        function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef)
                        and n.name == "run_full_flow_for_account")
        for stage in ("purchase_page_end", "sales_page_end"):
            with self.subTest(stage=stage):
                scope, driver, _ = load()
                body = next(n.orelse for n in ast.walk(function) if isinstance(n, ast.If)
                            and n.orelse and isinstance(n.orelse[0], ast.If)
                            and stage in ast.unparse(n.orelse[0].test))
                selected = [n for n in body if (
                    isinstance(n, ast.If) and (stage in ast.unparse(n.test)
                                               or "current_page_source == last_page_source" in ast.unparse(n.test))
                    or isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id in {
                        "current_page_source", "last_page_source"} for t in n.targets))]
                loop = ast.While(test=ast.Constant(True), body=selected + [ast.Break()], orelse=[])
                scope["last_page_source"] = "old-pre-pause-source"
                scope["_ui_pause_if_external"] = Mock(side_effect=[True, False, False])
                exec(compile(ast.fix_missing_locations(ast.Module(body=[loop], type_ignores=[])),
                             str(SOURCE), "exec"), scope)
                self.assertEqual(driver.source_calls, 1)
                self.assertEqual(scope["last_page_source"], "synthetic-rebound-source")

    def test_freight_empty_timeout_checks_external_before_return(self):
        scope, driver, _ = load()
        rv = SimpleNamespace(rect={})
        scope.update(FREIGHT_LIST_ID="com.ibb.tizi:id/rv", _freight_snapshot=Mock(),
                     correct_freight_clipped_visibility=Mock(), annotate_card_occurrences=lambda x: x)
        scope["wait_visible"] = Mock(side_effect=[TimeoutException(), rv])
        driver.packages = ["synthetic.external", "com.ibb.tizi"]
        driver.results = [[]]
        result = scope["get_visible_freight_snapshots"]("sheet", "kind", "value", "car")
        self.assertIs(result[0], rv)
        self.assertEqual(scope["wait_visible"].call_count, 2)

    def test_query_driver_error_not_converted_to_empty_in_wireless_mode(self):
        scope, _, _ = load()
        error = WebDriverException("synthetic driver failure")
        scope["click_step"] = Mock()
        scope["wait_visible"] = Mock(side_effect=error)
        with self.assertRaises(WebDriverException) as caught:
            scope["query_current_vehicle"]("synthetic")
        self.assertIs(caught.exception, error)


if __name__ == "__main__":
    unittest.main()
