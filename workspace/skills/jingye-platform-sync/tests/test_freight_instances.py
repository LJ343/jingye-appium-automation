from __future__ import annotations

import datetime as dt
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from jingye_store import (  # noqa: E402
    connect_db,
    export_pending_freight_seed,
    import_freight_history,
)


SHEET = "销售单-河北众沃"
SOURCE_KIND = "login_account"
SOURCE_VALUE = "10000000008"
WAYBILL = "YS20260828142047134"
CAR = "冀Z00014"


def target_event(
    waybill: str = WAYBILL,
    app_export_time: str = "2026-08-28 14:50:48",
) -> dict[str, object]:
    return {
        "version": 1,
        "event": "sales_target",
        "run_id": "test-run",
        "sheet": SHEET,
        "source_kind": SOURCE_KIND,
        "source_value": SOURCE_VALUE,
        "waybill": waybill,
        "car": CAR,
        "app_export_time": app_export_time,
        "recorded_at": "2026-08-31T12:00:00",
    }


def success_event(occurrence: int) -> dict[str, object]:
    return {
        "version": 1,
        "event": "freight_success",
        "run_id": "test-run",
        "sheet": SHEET,
        "source_kind": SOURCE_KIND,
        "source_value": SOURCE_VALUE,
        "waybill": WAYBILL,
        "car": CAR,
        "outbound_datetime": "2026-08-28 16:37:12",
        "raw_spec": "1.5mm*1200mm*C",
        "normalized_spec": "1.5*1200*C",
        "settlement_quantity": 16.9,
        "unit_price": 75.0,
        "card_occurrence": occurrence,
        "record_occurrence": occurrence,
        "recorded_at": "2026-08-31T12:01:00",
    }


def scope_event(*, completed: bool) -> dict[str, object]:
    return {
        "version": 1,
        "event": "freight_scope_complete",
        "run_id": "test-run",
        "sheet": SHEET,
        "source_kind": SOURCE_KIND,
        "source_value": SOURCE_VALUE,
        "target_count": 1,
        "success_count": 2 if completed else 1,
        "missing_waybills": [],
        "completed_waybills": [WAYBILL] if completed else [],
        "failed_waybills": [] if completed else [WAYBILL],
        "failed_card_count": 0 if completed else 1,
        "recorded_at": "2026-08-31T12:02:00",
    }


class FreightInstanceStoreTest(unittest.TestCase):
    def write_events(self, base_dir: Path, events: list[dict[str, object]]) -> None:
        target = base_dir / "freight_confirmation" / "test-run.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in events),
            encoding="utf-8",
        )

    def test_identical_cards_are_stored_twice_and_reimport_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            connection = connect_db(base_dir / "facts.sqlite3")
            try:
                self.write_events(
                    base_dir,
                    [target_event(), success_event(1), success_event(2), scope_event(completed=True)],
                )

                import_freight_history(connection, base_dir)
                import_freight_history(connection, base_dir)

                rows = connection.execute(
                    "SELECT record_key, record_occurrence FROM freight_records ORDER BY record_occurrence"
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertEqual([row["record_occurrence"] for row in rows], [1, 2])
                self.assertNotEqual(rows[0]["record_key"], rows[1]["record_key"])
                status = connection.execute(
                    "SELECT status FROM freight_targets WHERE waybill_no = ?", (WAYBILL,)
                ).fetchone()[0]
                self.assertEqual(status, "COMPLETE")
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM schema_meta WHERE key='schema_version'"
                    ).fetchone()[0],
                    "9",
                )
            finally:
                connection.close()

    def test_one_success_and_one_instance_failure_stays_partial(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            connection = connect_db(base_dir / "facts.sqlite3")
            try:
                failure = {
                    **success_event(2),
                    "event": "freight_failure",
                    "error": "目标运费卡片详情读取失败",
                }
                failure.pop("settlement_quantity")
                failure.pop("unit_price")
                self.write_events(
                    base_dir,
                    [target_event(), success_event(1), failure, scope_event(completed=False)],
                )

                import_freight_history(connection, base_dir)

                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM freight_records").fetchone()[0], 1
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM freight_failures").fetchone()[0], 1
                )
                status = connection.execute(
                    "SELECT status FROM freight_targets WHERE waybill_no = ?", (WAYBILL,)
                ).fetchone()[0]
                self.assertEqual(status, "PARTIAL")
            finally:
                connection.close()

    def test_interrupted_scope_ignores_repeated_identical_fact(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            connection = connect_db(base_dir / "facts.sqlite3")
            try:
                self.write_events(
                    base_dir,
                    [target_event(), success_event(1), success_event(2)],
                )

                stats = import_freight_history(connection, base_dir)

                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM freight_records").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    stats.get("incomplete_duplicate_success_events_ignored"),
                    1,
                )
                status = connection.execute(
                    "SELECT status FROM freight_targets WHERE waybill_no = ?", (WAYBILL,)
                ).fetchone()[0]
                self.assertEqual(status, "PARTIAL")
            finally:
                connection.close()

    def test_pending_seed_includes_day_14_but_skips_day_15(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            connection = connect_db(base_dir / "facts.sqlite3")
            try:
                self.write_events(
                    base_dir,
                    [
                        target_event(
                            "YS20260828142047134",
                            "2026-08-01 00:00:00",
                        ),
                        target_event("YS20260827142047134"),
                        target_event(
                            "YSLEGACY001",
                            "2026-08-27 14:50:48",
                        ),
                        target_event("YSUNKNOWN", ""),
                    ],
                )
                import_freight_history(connection, base_dir)

                output_path = base_dir / "seed.jsonl"
                stats = export_pending_freight_seed(
                    connection,
                    output_path,
                    "seed-run",
                    anchor_date=dt.date(2026, 9, 11),
                )
                events = [
                    json.loads(line)
                    for line in output_path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
                seeded_waybills = {
                    str(event.get("waybill") or "")
                    for event in events
                    if event.get("event") == "sales_target"
                }

                self.assertEqual(
                    seeded_waybills,
                    {"YS20260828142047134", "YSUNKNOWN"},
                )
                self.assertEqual(stats["targets_seeded"], 2)
                self.assertEqual(stats["targets_skipped_by_age"], 2)
            finally:
                connection.close()


if __name__ == "__main__":
    unittest.main()
