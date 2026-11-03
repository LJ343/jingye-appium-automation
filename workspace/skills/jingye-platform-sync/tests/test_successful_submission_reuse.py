from __future__ import annotations

import hashlib
import sqlite3
import sys
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import jingye_sync  # noqa: E402
from jingye_store import Candidate, canonical_json  # noqa: E402


class FakeResponse:
    status_code = 200
    text = ""

    @staticmethod
    def json() -> dict[str, object]:
        return {
            "code": 0,
            "message": "ok",
            "data": {"waybillId": "remote-new"},
        }


class FakeSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.posts: list[dict[str, object]] = []

    def post(self, url: str, *, data: bytes, timeout: tuple[float, float]) -> FakeResponse:
        self.posts.append({"url": url, "data": data, "timeout": timeout})
        return FakeResponse()


def payload(request_id: str, waybill_no: str, unloading_point: str) -> dict[str, object]:
    return {
        "requestId": request_id,
        "sourceSystem": "agent",
        "externalWaybillNo": waybill_no,
        "unloadingPointName": unloading_point,
        "loadingTicket": {"externalTicketNo": f"T-{waybill_no}"},
    }


class SuccessfulSubmissionReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE submissions (
                request_id TEXT PRIMARY KEY,
                waybill_id INTEGER NOT NULL UNIQUE,
                source_system TEXT NOT NULL,
                external_waybill_no TEXT NOT NULL,
                external_ticket_no TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                http_status INTEGER,
                response_code INTEGER,
                response_message TEXT,
                response_json TEXT,
                platform_waybill_id TEXT,
                first_prepared_at TEXT NOT NULL,
                last_attempt_at TEXT,
                success_at TEXT
            );
            CREATE TABLE upload_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                attempted_at TEXT NOT NULL,
                method TEXT NOT NULL,
                http_status INTEGER,
                response_code INTEGER,
                state TEXT NOT NULL,
                response_message TEXT NOT NULL DEFAULT '',
                response_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE submission_revisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                waybill_id INTEGER NOT NULL,
                revision_no INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                http_status INTEGER,
                response_code INTEGER,
                response_message TEXT,
                response_json TEXT,
                archived_at TEXT NOT NULL,
                reason TEXT NOT NULL
            );
            """
        )

    def tearDown(self) -> None:
        self.connection.close()

    def test_successful_drift_is_frozen_and_next_new_candidate_uploads(self) -> None:
        frozen_payload = payload("AGENT-JY-PURCHASE-OLD", "OLD", "原卸货点")
        frozen_json = canonical_json(frozen_payload)
        self.connection.execute(
            """
            INSERT INTO submissions(
                request_id, waybill_id, source_system, external_waybill_no,
                external_ticket_no, payload_json, payload_sha256, status,
                first_prepared_at, success_at
            ) VALUES (?, 1, 'agent', 'OLD', 'T-OLD', ?, ?, 'SUCCESS', ?, ?)
            """,
            (
                frozen_payload["requestId"],
                frozen_json,
                hashlib.sha256(frozen_json.encode("utf-8")).hexdigest(),
                "2026-08-01T00:00:00",
                "2026-08-01T00:01:00",
            ),
        )
        self.connection.commit()

        candidates = [
            Candidate(
                waybill_id=1,
                direction="PURCHASE",
                waybill_no="OLD",
                payload=payload("AGENT-JY-PURCHASE-OLD", "OLD", "新卸货点"),
            ),
            Candidate(
                waybill_id=2,
                direction="SALES",
                waybill_no="NEW",
                payload=payload("AGENT-JY-SALES-NEW", "NEW", "新目的地"),
            ),
        ]
        args = Namespace(
            execute=True,
            confirm_project="敬业钢铁",
            project_name="敬业钢铁",
            waybill=[],
            allow_batch=True,
            limit=0,
            replace_rejected_route=False,
            base_url="https://platform.invalid",
            allow_http=False,
            api_key_env="TEST_API_KEY",
            timeout=30.0,
        )
        session = FakeSession()

        with (
            mock.patch.object(jingye_sync, "candidate_rows", return_value=candidates),
            mock.patch.object(
                jingye_sync,
                "validate_remote_options",
                return_value=("https://platform.invalid", "test-key"),
            ),
            mock.patch.object(jingye_sync.requests, "Session", return_value=session),
            self.assertLogs("jingye-platform-sync", level="WARNING") as captured,
        ):
            result = jingye_sync.upload_candidates(self.connection, args)

        self.assertEqual(result["stats"]["skipped_success"], 1)
        self.assertEqual(result["stats"]["success_payload_drift"], 1)
        self.assertEqual(result["stats"]["success"], 1)
        self.assertEqual(len(session.posts), 1)
        self.assertIn("changedFields=unloadingPointName", "\n".join(captured.output))
        self.assertEqual(
            result["results"][0],
            {
                "requestId": "AGENT-JY-PURCHASE-OLD",
                "state": "SKIPPED_SUCCESS_PAYLOAD_DRIFT",
                "changedFields": ["unloadingPointName"],
            },
        )

        frozen_row = self.connection.execute(
            "SELECT payload_json, status, attempts FROM submissions WHERE waybill_id=1"
        ).fetchone()
        self.assertEqual(frozen_row["payload_json"], frozen_json)
        self.assertEqual(frozen_row["status"], "SUCCESS")
        self.assertEqual(frozen_row["attempts"], 0)

        new_row = self.connection.execute(
            "SELECT status, attempts FROM submissions WHERE waybill_id=2"
        ).fetchone()
        self.assertEqual(new_row["status"], "SUCCESS")
        self.assertEqual(new_row["attempts"], 1)


if __name__ == "__main__":
    unittest.main()
