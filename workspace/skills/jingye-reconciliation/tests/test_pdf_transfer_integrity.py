"""Offline AST-isolated tests: importing yyb_search itself would start business work."""
from __future__ import annotations

import ast
import contextlib
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "yyb_search.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))
FUNCTIONS = {
    "_pdf_local_io_path",
    "_pdf_identity", "_pdf_local_facts", "_phone_pdf_sha256",
    "_pdf_integrity_metadata", "require_pdf_downloads_complete",
    "require_single_pdf_download", "pull_phone_pdf", "find_existing_archived_pdf",
    "_read_pdf_manifest", "_write_pdf_manifest", "register_expected_pdf_slots",
    "complete_expected_pdf_slots", "assert_all_pdf_batches_complete",
    "register_pdf_enumeration_pending",
    "run_device_adb",
    "execute_single_download",
}
PDF = b"%PDF-1.4\n" + b"offline-fixture\n" * 150 + b"%%EOF\n"


class FakeAdb:
    def __init__(self, mode="ok"):
        self.remote = PDF
        self.mode = mode
        self.calls = []

    def run(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[3:5] == ["shell", "sha256sum"]:
            return SimpleNamespace(stdout=hashlib.sha256(self.remote).hexdigest() + "  fixture.pdf\n")
        if command[3] != "pull":
            raise AssertionError("Unexpected external command")
        target = Path(command[-1])
        if self.mode in {"interrupt", "timeout"}:
            target.write_bytes(PDF[:1100])
            if self.mode == "timeout":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            raise subprocess.CalledProcessError(1, command)
        content = self.remote
        if self.mode == "truncated":
            content = self.remote[:-300] + b"%%EOF\n"
        elif self.mode == "equal_length_corruption":
            content = self.remote[:300] + b"X" + self.remote[301:]
        target.write_bytes(content)
        if self.mode == "remote_changed":
            self.remote = self.remote[:400] + b"Y" + self.remote[401:]
        return SimpleNamespace(stdout="")


def namespace(adb):
    class AccountError(RuntimeError):
        pass
    class OwnershipError(RuntimeError):
        pass
    class Unavailable(RuntimeError):
        pass
    scope = {
        "os": os, "re": re, "json": json, "hashlib": hashlib,
        "datetime": datetime, "subprocess": adb, "ADB_SERIAL": "TEST-DEVICE",
        "shlex": shlex, "wireless_link": SimpleNamespace(is_enabled=lambda: False, ConnectionUnavailable=Unavailable),
        "AppiumSessionOwnershipError": OwnershipError,
        "AccountSessionSuspectedError": AccountError,
        "clean_val": lambda value: str(value).strip(),
        "wait_until_phone_pdf_complete": lambda *a, **k: len(adb.remote),
        "get_phone_file_size": lambda *a: len(adb.remote),
        "append_source_context": Mock(),
    }
    nodes = [node for node in TREE.body if (
        isinstance(node, ast.FunctionDef) and node.name in FUNCTIONS
        or isinstance(node, ast.ClassDef) and node.name in {
            "PdfTransferValidationError", "PdfDownloadIncompleteError"}
    )]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), scope)
    return scope


def business_fragment(function_name, required_call, start_counter=False):
    """Run the actual fail/complete boundary, with surrounding UI replaced by mocks."""
    function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == function_name)
    for parent in ast.walk(function):
        statements = getattr(parent, "body", None)
        if not isinstance(statements, list):
            continue
        for index, statement in enumerate(statements):
            if not (isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
                    and isinstance(statement.value.func, ast.Name)
                    and statement.value.func.id == required_call):
                continue
            start = index
            if start_counter:
                starts = [i for i in range(index) if isinstance(statements[i], ast.Assign)
                          and any(isinstance(t, ast.Name) and t.id == "download_success_count"
                                  for t in statements[i].targets)]
                start = starts[-1]
            end = index + 1
            for j in range(index + 1, len(statements)):
                if any(isinstance(c, ast.Call) and (
                    isinstance(c.func, ast.Attribute) and c.func.attr == "add"
                    or isinstance(c.func, ast.Name) and c.func.id == "write_data_to_excel"
                ) for c in ast.walk(statements[j])):
                    end = j + 1
                    break
            return compile(ast.Module(body=statements[start:end], type_ignores=[]), str(SOURCE), "exec")
    raise AssertionError("Business completeness boundary not found")


class PdfTransferIntegrityTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.silence = contextlib.redirect_stdout(io.StringIO())
        self.silence.__enter__()
        self.addCleanup(self.silence.__exit__, None, None, None)

    def pull(self, adb):
        scope = namespace(adb)
        result = scope["pull_phone_pdf"]("/diagnostic/fixture.pdf", "WB-TEST", "CAR-TEST", str(self.folder), "1")
        return scope, result

    def test_success_is_atomic_and_records_remote_and_local_hashes(self):
        adb = FakeAdb()
        scope, ok = self.pull(adb)
        self.assertTrue(ok)
        pdf = next(self.folder.glob("*.pdf"))
        meta = json.loads(Path(str(pdf) + ".integrity.json").read_text())
        self.assertEqual(pdf.read_bytes(), PDF)
        self.assertEqual(meta["sha256"], hashlib.sha256(PDF).hexdigest())
        self.assertEqual(meta["remoteSHA256Before"], meta["remoteSHA256After"])
        self.assertFalse(list(self.folder.glob("*.part")))
        self.assertEqual(scope["append_source_context"].call_count, 1)
        self.assertEqual([call[1]["timeout"] for call in adb.calls], [30, 150, 30])

    def test_interrupt_truncation_corruption_timeout_never_publish(self):
        original = self.folder / "original.pdf"
        original.write_bytes(PDF)
        for mode in ("interrupt", "truncated", "equal_length_corruption", "timeout", "remote_changed"):
            with self.subTest(mode=mode):
                scope, ok = self.pull(FakeAdb(mode))
                self.assertFalse(ok)
                self.assertEqual(list(self.folder.glob("*.pdf")), [original])
                self.assertEqual(original.read_bytes(), PDF)
                self.assertFalse(list(self.folder.glob("*.part")))
                self.assertFalse(list(self.folder.glob("*.integrity.json")))
                scope["append_source_context"].assert_not_called()

    def test_stable_remote_truncated_pdf_is_not_complete(self):
        adb = FakeAdb()
        adb.remote = PDF[:-100]
        _, ok = self.pull(adb)
        self.assertFalse(ok)
        self.assertFalse(list(self.folder.glob("*.pdf")))

    def test_verified_duplicate_is_reused_but_corruption_and_wrong_index_are_not(self):
        adb = FakeAdb()
        scope, ok = self.pull(adb)
        self.assertTrue(ok)
        pdf = next(self.folder.glob("*.pdf"))
        calls = len(adb.calls)
        self.assertEqual(scope["find_existing_archived_pdf"](str(self.folder), "WB-TEST", "CAR-TEST", "1"), str(pdf))
        self.assertEqual(len(adb.calls), calls)
        self.assertEqual(scope["find_existing_archived_pdf"](str(self.folder), "WB-TEST", "CAR-TEST", "2"), "")
        content = pdf.read_bytes()
        pdf.write_bytes(content[:250] + b"Z" + content[251:])
        self.assertEqual(scope["find_existing_archived_pdf"](str(self.folder), "WB-TEST", "CAR-TEST", "1"), "")

    def test_legacy_archive_without_digest_is_preserved_not_blindly_reused(self):
        scope = namespace(FakeAdb())
        pdf = self.folder / "WB-TEST_CAR-TEST_20261103_000000_1.pdf"
        pdf.write_bytes(PDF)
        self.assertEqual(scope["find_existing_archived_pdf"](str(self.folder), "WB-TEST", "CAR-TEST", "1"), "")
        self.assertEqual(pdf.read_bytes(), PDF)

    def test_purchase_failure_does_not_mark_either_path_processed(self):
        for name in ("process_purchase_current_vehicle", "run_full_flow_for_account"):
            with self.subTest(function=name):
                scope = namespace(FakeAdb())
                scope.update(driver=object(), wait=object(), wb_no="WB-TEST", info={"\u8f66\u724c\u53f7": "CAR-TEST"},
                             pounds_text_label=object(), pdf_dir=str(self.folder), PDF_PURCHASE_DIR=str(self.folder),
                             selected_car="CAR-TEST", phone="TEST-ACCOUNT", sheet_name="TEST-SHEET",
                             processed_waybills=set(), processed_waybill_order=[], click_back_to_list=Mock(),
                             execute_single_download=Mock(return_value=False))
                with self.assertRaises(scope["PdfDownloadIncompleteError"]):
                    exec(business_fragment(name, "require_single_pdf_download"), scope)
                self.assertFalse(scope["processed_waybills"])
                scope["click_back_to_list"].assert_not_called()

    def test_one_missing_of_three_sales_attachments_blocks_both_complete_paths(self):
        for name in ("process_sales_current_vehicle", "run_full_flow_for_account"):
            with self.subTest(function=name):
                scope = namespace(FakeAdb())
                scope.update(driver=SimpleNamespace(find_elements=Mock(return_value=[object(), object(), object()])),
                             wait=object(), total_pics=3, order_no="WB-TEST", selected_car="CAR-TEST",
                             global_car_no="CAR-TEST", pdf_dir=str(self.folder), PDF_SALES_DIR=str(self.folder),
                             AppiumBy=SimpleNamespace(ID="id"), phone="TEST-ACCOUNT", sheet_name="TEST-SHEET",
                             info={}, write_data_to_excel=Mock(), execute_single_download=Mock(side_effect=[True, False, True]))
                with self.assertRaises(scope["PdfDownloadIncompleteError"]):
                    exec(business_fragment(name, "require_pdf_downloads_complete", start_counter=True), scope)
                scope["write_data_to_excel"].assert_not_called()

    def test_partial_manifest_cannot_be_erased_by_zero_or_smaller_retry(self):
        scope = namespace(FakeAdb())
        scope["register_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST", 3, indexed=True)
        for count in (0, 1, 2):
            with self.subTest(count=count), self.assertRaises(scope["PdfDownloadIncompleteError"]):
                scope["register_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST", count, indexed=True)
        with self.assertRaises(scope["PdfDownloadIncompleteError"]):
            scope["assert_all_pdf_batches_complete"]([str(self.folder)])

    def test_manifest_completes_only_all_expected_slots_then_detects_later_corruption(self):
        scope = namespace(FakeAdb())
        scope["register_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST", 2, indexed=True)
        self.assertTrue(scope["pull_phone_pdf"]("/diagnostic/fixture.pdf", "WB-TEST", "CAR-TEST", str(self.folder), "1"))
        with self.assertRaises(scope["PdfDownloadIncompleteError"]):
            scope["complete_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST")
        self.assertTrue(scope["pull_phone_pdf"]("/diagnostic/fixture.pdf", "WB-TEST", "CAR-TEST", str(self.folder), "2"))
        scope["complete_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST")
        scope["assert_all_pdf_batches_complete"]([str(self.folder)])
        pdf = next(self.folder.glob("*.pdf"))
        content = pdf.read_bytes()
        pdf.write_bytes(content[:200] + b"Q" + content[201:])
        with self.assertRaises(scope["PdfDownloadIncompleteError"]):
            scope["assert_all_pdf_batches_complete"]([str(self.folder)])

    def test_real_duplicate_download_entry_reuses_verified_slot_without_adb_or_ui(self):
        adb = FakeAdb()
        scope = namespace(adb)
        self.assertTrue(scope["pull_phone_pdf"]("/diagnostic/fixture.pdf", "WB-TEST", "CAR-TEST", str(self.folder)))
        command_count = len(adb.calls)
        for _ in range(2):
            self.assertTrue(scope["require_single_pdf_download"](
                object(), object(), "WB-TEST", "CAR-TEST", object(), str(self.folder)))
        self.assertEqual(len(adb.calls), command_count)
        self.assertEqual(len(list(self.folder.glob("*.pdf"))), 1)
        scope["assert_all_pdf_batches_complete"]([str(self.folder)])

    def test_wireless_wait_budget_failure_is_not_downgraded_to_false(self):
        class Unavailable(RuntimeError):
            pass
        scope = namespace(FakeAdb())
        scope["wireless_link"] = SimpleNamespace(is_enabled=lambda: True, ConnectionUnavailable=Unavailable,
                                                 run_adb=Mock(side_effect=Unavailable("wait_budget")))
        with self.assertRaises(Unavailable):
            scope["pull_phone_pdf"]("/diagnostic/fixture.pdf", "WB-TEST", "CAR-TEST", str(self.folder))
        self.assertFalse(list(self.folder.glob("*.pdf")))

    def test_sales_attachment_enumeration_error_is_never_a_zero_attachment_result(self):
        for name in ("process_sales_current_vehicle", "run_full_flow_for_account"):
            with self.subTest(function=name):
                function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
                block = next(n for n in ast.walk(function) if isinstance(n, ast.Try) and any(
                    isinstance(statement, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "pic_links"
                                                            for t in statement.targets)
                    for statement in n.body))
                scope = namespace(FakeAdb())
                scope.update(driver=SimpleNamespace(find_elements=Mock(side_effect=RuntimeError("fixture"))),
                             AppiumBy=SimpleNamespace(ID="id"), selected_car="CAR-TEST", total_pics=None)
                with self.assertRaises(scope["PdfDownloadIncompleteError"]):
                    exec(compile(ast.Module(body=[block], type_ignores=[]), str(SOURCE), "exec"), scope)
                self.assertIsNone(scope["total_pics"])

    def test_first_observed_zero_does_not_invent_required_slots(self):
        scope = namespace(FakeAdb())
        scope["register_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST", 0, indexed=True)
        self.assertFalse((self.folder / ".pdf-download-manifest.json").exists())

    def test_real_download_wrapper_preserves_control_failure_and_stops_cleanup(self):
        for failure_kind in ("wireless", "ownership", "cleanup_uncertain"):
            with self.subTest(failure_kind=failure_kind):
                scope = namespace(FakeAdb())
                driver = SimpleNamespace(switch_to=SimpleNamespace(context=Mock()), terminate_app=Mock())
                scope.update(
                    get_latest_phone_pdf=Mock(return_value=""),
                    tap_clickable_span_shotgun=Mock(),
                    wait_for_browser_or_pdf=Mock(return_value=("pdf", "/diagnostic/fixture.pdf")),
                    is_appium_session_lost=Mock(side_effect=AssertionError("control failure must not be reclassified")),
                    close_current_honor_pdf_page=Mock(), activate_tizi_and_wait=Mock(),
                )
                if failure_kind == "wireless":
                    error = scope["wireless_link"].ConnectionUnavailable("wait_budget")
                    scope["wireless_link"].is_enabled = lambda: True
                    scope["wireless_link"].run_adb = Mock(side_effect=error)
                else:
                    cls = scope["AppiumSessionOwnershipError"]
                    if failure_kind == "cleanup_uncertain":
                        cls = type("SessionCleanupUncertain", (cls,), {})
                    error = cls("owned_session_uncertain")
                    scope["tap_clickable_span_shotgun"].side_effect = error
                with self.assertRaises(type(error)) as caught:
                    scope["execute_single_download"](
                        driver, object(), "WB-TEST", "CAR-TEST", object(), str(self.folder))
                self.assertIs(caught.exception, error)
                self.assertEqual(driver.switch_to.context.call_count, 1)
                self.assertEqual(scope["tap_clickable_span_shotgun"].call_count, 1)
                driver.terminate_app.assert_not_called()
                scope["close_current_honor_pdf_page"].assert_not_called()
                scope["activate_tizi_and_wait"].assert_not_called()
                scope["is_appium_session_lost"].assert_not_called()

    def test_purchase_observation_is_registered_before_excel_or_detail_failure(self):
        for name in ("process_purchase_current_vehicle", "run_full_flow_for_account"):
            with self.subTest(function=name):
                function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
                body = next(n.body for n in ast.walk(function) if hasattr(n, "body") and isinstance(n.body, list)
                            and any(isinstance(x, ast.Expr) and isinstance(x.value, ast.Call)
                                    and isinstance(x.value.func, ast.Name) and x.value.func.id == "write_data_to_excel"
                                    and any(k.arg == "is_sales" and isinstance(k.value, ast.Constant) and k.value.value is False
                                            for k in x.value.keywords) for x in n.body))
                index = next(i for i, x in enumerate(body) if isinstance(x, ast.Expr) and isinstance(x.value, ast.Call)
                             and isinstance(x.value.func, ast.Name) and x.value.func.id == "register_expected_pdf_slots")
                scope = namespace(FakeAdb())
                error = RuntimeError("detail_click_failed")
                scope.update(info={"\u8f66\u724c\u53f7": "CAR-TEST"}, wb_no="WB-TEST", selected_car="CAR-TEST",
                             sheet_name="TEST-SHEET", phone="TEST-ACCOUNT", pdf_dir=str(self.folder),
                             PDF_PURCHASE_DIR=str(self.folder), write_data_to_excel=Mock(), written_count=0,
                             btn_els=[SimpleNamespace(click=Mock(side_effect=error))])
                end = next(i + 1 for i in range(index, len(body)) if any(isinstance(c, ast.Call)
                           and isinstance(c.func, ast.Attribute) and c.func.attr == "click" for c in ast.walk(body[i])))
                with self.assertRaises(RuntimeError):
                    exec(compile(ast.Module(body=body[index:end], type_ignores=[]), str(SOURCE), "exec"), scope)
                scope["write_data_to_excel"].assert_called_once()
                with self.assertRaises(scope["PdfDownloadIncompleteError"]):
                    scope["assert_all_pdf_batches_complete"]([str(self.folder)])

    def test_sales_detail_failure_cannot_disappear_on_empty_retry(self):
        scope = namespace(FakeAdb())
        for name in ("process_sales_current_vehicle", "run_full_flow_for_account"):
            function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
            marker = next(n for n in ast.walk(function) if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Name) and n.func.id == "register_pdf_enumeration_pending")
            click = min((n for n in ast.walk(function) if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute) and n.func.attr == "click"
                         and n.lineno > marker.lineno), key=lambda n: n.lineno)
            self.assertGreater(click.lineno, marker.lineno)
        scope["register_pdf_enumeration_pending"](str(self.folder), "WB-TEST", "CAR-TEST")
        with self.assertRaises(scope["PdfDownloadIncompleteError"]):
            scope["assert_all_pdf_batches_complete"]([str(self.folder)])
        scope["register_expected_pdf_slots"](str(self.folder), "WB-TEST", "CAR-TEST", 0, indexed=True)
        scope["assert_all_pdf_batches_complete"]([str(self.folder)])

    def test_stage_recovery_preserves_control_error_without_debug_or_ui_replay(self):
        for name in ("run_freight_scope_with_recovery", "run_shanghai_sales_stage_with_recovery"):
            for kind in ("wireless", "ownership", "cleanup_uncertain"):
                with self.subTest(stage=name, kind=kind):
                    scope = namespace(FakeAdb())
                    cls = (scope["wireless_link"].ConnectionUnavailable if kind == "wireless"
                           else scope["AppiumSessionOwnershipError"])
                    if kind == "cleanup_uncertain":
                        cls = type("SessionCleanupUncertain", (cls,), {})
                    error = cls("control_failure")
                    forbidden = {key: Mock(side_effect=AssertionError("no action after control error")) for key in (
                        "is_appium_session_lost", "is_account_relogin_error", "dump_freight_debug_artifacts",
                        "activate_tizi_and_wait", "recreate_appium_session", "restart_app_keep_state",
                        "login_with_account", "select_car_from_spinner", "query_current_vehicle")}
                    scope.update(forbidden, FREIGHT_SCOPE_MAX_ATTEMPTS=3,
                                 SHANGHAI_CARS=["CAR-TEST"], SHANGHAI_SALES_VEHICLE_MAX_ATTEMPTS=3,
                                 process_freight_scope=Mock(side_effect=error),
                                 enter_sales_page_without_query=Mock(side_effect=error))
                    function = next(n for n in TREE.body if isinstance(n, ast.FunctionDef) and n.name == name)
                    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), scope)
                    args = ("TEST-ACCOUNT", "TEST-SHEET", "selected_car", "CAR-TEST", "CAR-TEST") if name.startswith("run_freight") else ("TEST-ACCOUNT",)
                    with self.assertRaises(cls) as caught:
                        scope[name](*args)
                    self.assertIs(caught.exception, error)
                    for action in forbidden.values():
                        action.assert_not_called()


if __name__ == "__main__":
    unittest.main()
