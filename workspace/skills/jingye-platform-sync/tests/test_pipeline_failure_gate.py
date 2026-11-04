"""AST-isolated pipeline failure ordering; no DB, email or subprocess execution."""
from __future__ import annotations

import ast
import contextlib
import io
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock


SOURCE = Path(__file__).resolve().parents[1] / "scripts/jingye_pipeline.py"
TREE = ast.parse(SOURCE.read_text(encoding="utf-8"))


class PipelineFailureGateTest(unittest.TestCase):
    def execute(self, crawl_code=0, refresh_code=0):
        temp = tempfile.TemporaryDirectory(prefix="pipeline-gate-offline-")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        events = []
        commands = [(name, ["OFFLINE-NOT-EXECUTED", name]) for name in (
            "create_excel", "crawl", "refresh", "attachments", "oss_upload", "validate_upload", "platform_upload")]

        def step(name, command):
            events.append(name)
            code = crawl_code if name == "crawl" else refresh_code if name == "refresh" else 0
            return SimpleNamespace(name=name, return_code=code, output_tail=[],
                                   as_dict=lambda: {"name": name, "return_code": code})

        scope = {
            "single_instance_lock": lambda path: contextlib.nullcontext(),
            "record_run_start": Mock(), "read_status": Mock(return_value={}),
            "run_step": step, "seed_pending_freight": Mock(return_value=step("seed", [])),
            "update_crawl_scopes": Mock(side_effect=lambda *args: events.append("update_scopes")),
            "parse_step_json": Mock(return_value={"rejected": 0}),
            "read_upload_issue_details": Mock(return_value=[]),
            "excel_path_for": Mock(return_value=base / "unused.xlsx"),
            "extract_crawl_account_results": Mock(return_value=[]),
            "send_summary_email": Mock(return_value={"sent": True, "recipients": ["configured@example.invalid"]}),
            "record_run_finish": Mock(), "json": __import__("json"),
        }
        nodes = [ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)]
        nodes += [n for n in TREE.body if isinstance(n, ast.FunctionDef)
                  and n.name in {"require_success", "execute_pipeline"}]
        ast.fix_missing_locations(module := ast.Module(body=nodes, type_ignores=[]))
        exec(compile(module, str(SOURCE), "exec"), scope)
        events.clear()  # Creating the inert seed result above is not a pipeline command.
        windows = SimpleNamespace(anchor="2026-11-03", as_dict=lambda: {"anchor_date": "2026-11-03"})
        with contextlib.redirect_stdout(io.StringIO()):
            code = scope["execute_pipeline"](
                SimpleNamespace(email_to="configured@example.invalid"), windows,
                base, base / "runtime", base / "unused.sqlite3", "TEST-PROJECT", "TEST-RUN", commands,
                base / "unused.log")
        return code, events, scope

    def test_crawl_failure_still_refreshes_but_never_marks_or_uploads(self):
        code, events, scope = self.execute(crawl_code=1)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["create_excel", "crawl", "refresh"])
        scope["update_crawl_scopes"].assert_not_called()
        self.assertEqual(scope["record_run_finish"].call_args.args[2], "FAILED")
        self.assertEqual(scope["send_summary_email"].call_args.args[0]["status"], "FAILED")

    def test_refresh_failure_never_marks_or_uploads(self):
        code, events, scope = self.execute(refresh_code=1)
        self.assertEqual(code, 1)
        self.assertEqual(events, ["create_excel", "crawl", "refresh"])
        scope["update_crawl_scopes"].assert_not_called()
        self.assertEqual(scope["record_run_finish"].call_args.args[2], "FAILED")

    def test_success_retains_original_refresh_scope_upload_order(self):
        code, events, scope = self.execute()
        self.assertEqual(code, 0)
        self.assertEqual(events, ["create_excel", "crawl", "refresh", "update_scopes", "attachments",
                                  "oss_upload", "validate_upload", "platform_upload"])
        self.assertEqual(scope["record_run_finish"].call_args.args[2], "SUCCESS")
        scope["update_crawl_scopes"].assert_called_once()


if __name__ == "__main__":
    unittest.main()
