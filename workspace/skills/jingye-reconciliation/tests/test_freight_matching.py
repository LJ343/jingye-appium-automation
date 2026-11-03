from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import result_check  # noqa: E402
from freight_confirmation import (  # noqa: E402
    FreightData,
    FreightRecord,
    annotate_card_occurrences,
    freight_scan_past_target_date,
    freight_target_age_days,
    freight_target_within_retry_window,
    freight_record_key,
    next_card_occurrence,
    normalize_spec,
    waybill_date_part,
)


SHEET = "销售单-河北众沃"
SOURCE_KIND = "login_account"
SOURCE_VALUE = "test-account"
WAYBILL = "YSTEST001"


class FreightTraversalBoundaryTest(unittest.TestCase):
    def test_waybill_date_is_primary_scope_date(self) -> None:
        self.assertEqual(waybill_date_part("YS20260828142047134"), "2026-08-28")

    def test_same_target_date_does_not_end_scan(self) -> None:
        self.assertFalse(
            freight_scan_past_target_date("2026-08-28 23:59:59", "2026-08-28")
        )

    def test_strictly_older_card_ends_descending_scan(self) -> None:
        self.assertTrue(
            freight_scan_past_target_date("2026-08-27 23:59:59", "2026-08-28")
        )

    def test_newer_or_unknown_card_does_not_end_scan(self) -> None:
        self.assertFalse(
            freight_scan_past_target_date("2026-08-30 15:33:00", "2026-08-28")
        )
        self.assertFalse(freight_scan_past_target_date("", "2026-08-28"))

    def test_retry_window_includes_day_14_and_excludes_day_15(self) -> None:
        self.assertEqual(
            freight_target_age_days(
                "YS20260828142047134",
                "2026-08-01 00:00:00",
                "2026-09-11",
            ),
            14,
        )
        self.assertTrue(
            freight_target_within_retry_window(
                "YS20260828142047134",
                "2026-08-01 00:00:00",
                "2026-09-11",
            )
        )
        self.assertFalse(
            freight_target_within_retry_window(
                "YS20260827142047134",
                "2026-09-01 00:00:00",
                "2026-09-11",
            )
        )

    def test_unknown_target_date_remains_eligible(self) -> None:
        self.assertTrue(
            freight_target_within_retry_window(
                "YSUNKNOWN",
                "",
                "2026-09-11",
            )
        )


def task() -> result_check.RowTask:
    return result_check.RowTask(
        sheet_name=SHEET,
        kind="sales",
        row=2,
        sequence=1,
        waybill=WAYBILL,
        car="",
        goods="",
        destination="",
        load_weight=None,
        unload_weight=None,
        pdfs=[],
        related_pdfs=[],
        matching_notes=[],
        source_detail="",
        source_keys=((SOURCE_KIND, SOURCE_VALUE),),
    )


def detail(
    spec: str,
    theoretical_weight: float,
    detail_index: int,
    event_datetime: str = "2026-08-23 02:39:38",
) -> result_check.SalesDetailRow:
    return result_check.SalesDetailRow(
        pdf_index=1,
        detail_index=detail_index,
        match_event_datetime=event_datetime,
        spec=spec,
        theoretical_weight=theoretical_weight,
    )


def record(
    key: str,
    settlement_quantity: float,
    outbound_datetime: str = "2026-08-23 02:14:05",
    raw_spec: str = "1mm*1250mm*C",
) -> FreightRecord:
    return FreightRecord(
        record_key=key,
        card_key=f"card-{key}",
        sheet=SHEET,
        source_kind=SOURCE_KIND,
        source_value=SOURCE_VALUE,
        car="",
        waybill=WAYBILL,
        outbound_datetime=outbound_datetime,
        raw_spec=raw_spec,
        normalized_spec=normalize_spec(raw_spec),
        settlement_quantity=settlement_quantity,
        unit_price=70.0,
    )


def freight_data(*records: FreightRecord) -> FreightData:
    return FreightData(records={item.record_key: item for item in records})


class FreightMatchingTest(unittest.TestCase):
    def test_unique_same_date_weight_resolves_unequal_same_spec_candidates(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 24.67, 1),
            detail("1mm*1250mm*C,1件", 8.39, 2),
        ]
        freight_record = record("record-1", 24.67)

        issues, handled, applicable, pending = result_check.match_freight_to_sales_details(
            task(), details, freight_data(freight_record)
        )

        self.assertTrue(applicable)
        self.assertEqual(pending, "")
        self.assertEqual(handled, {"record-1"})
        self.assertIs(details[0].freight_record, freight_record)
        self.assertEqual(details[0].settlement_quantity, 24.67)
        self.assertEqual(details[0].unit_price, 70.0)
        self.assertIsNone(details[1].freight_record)
        self.assertFalse(any("规格匹配歧义" in issue for issue in issues))
        self.assertEqual(
            issues,
            ["运费确认未匹配到规格“1mm*1250mm*C,1件”（附件1第2组）"],
        )

    def test_duplicate_weight_candidates_remain_ambiguous(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 24.67, 1),
            detail("1mm*1250mm*C,1件", 24.67, 2),
        ]
        freight_record = record("record-1", 24.67)

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(freight_record)
            )
        )

        self.assertIsNone(details[0].freight_record)
        self.assertIsNone(details[1].freight_record)
        self.assertEqual(len(details), 3)
        self.assertIs(details[2].freight_record, freight_record)
        self.assertEqual(details[2].spec, freight_record.raw_spec)
        self.assertEqual(details[2].settlement_quantity, 24.67)
        self.assertEqual(details[2].unit_price, 70.0)
        self.assertIn(
            "暂未匹配到PDF规格",
            result_check.sales_detail_comment(details[2]),
        )
        self.assertEqual(sum("规格匹配歧义" in issue for issue in issues), 2)
        self.assertTrue(any("记录未匹配到PDF规格" in issue for issue in issues))

    def test_no_pdf_still_preserves_extracted_freight_values(self) -> None:
        freight_record = record("record-1", 34.2)
        handled: set[str] = set()

        result = result_check.audit_sales_row(
            task(),
            {},
            freight_data(freight_record),
            handled,
        )

        self.assertEqual(result["status"], "异常")
        self.assertTrue(result["freight_applicable"])
        self.assertEqual(handled, {"record-1"})
        self.assertEqual(len(result["details"]), 1)
        self.assertEqual(result["details"][0].settlement_quantity, 34.2)
        self.assertEqual(result["details"][0].unit_price, 70.0)
        self.assertIn("记录未匹配到PDF规格", result["remark"])

    def test_equal_candidate_counts_use_weight_not_original_order(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 24.67, 1),
            detail("1mm*1250mm*C,1件", 8.39, 2),
        ]
        first_record = record("record-1", 8.39)
        second_record = record("record-2", 24.67)

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(first_record, second_record)
            )
        )

        self.assertEqual(issues, [])
        self.assertIs(details[0].freight_record, second_record)
        self.assertIs(details[1].freight_record, first_record)

    def test_equivalent_duplicate_records_can_fill_equal_count_rows(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 24.67, 1),
            detail("1mm*1250mm*C,1件", 8.39, 2),
        ]
        first_record = record("record-1", 16.9)
        second_record = record("record-2", 16.9)

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(first_record, second_record)
            )
        )

        self.assertEqual(issues, [])
        self.assertIs(details[0].freight_record, first_record)
        self.assertIs(details[1].freight_record, second_record)

    def test_surplus_equivalent_record_is_collapsed_to_one_pdf_row(self) -> None:
        details = [detail("1mm*1250mm*C,1\u4ef6", 34.2, 1)]
        first_record = record("record-1", 34.2)
        duplicate_record = record("record-2", 34.2)

        issues, handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(first_record, duplicate_record)
            )
        )

        self.assertEqual(issues, [])
        self.assertEqual(handled, {"record-1", "record-2"})
        self.assertEqual(len(details), 1)
        self.assertIs(details[0].freight_record, first_record)

    def test_two_pdf_rows_keep_two_equivalent_records_and_collapse_only_surplus(self) -> None:
        details = [
            detail("1mm*1250mm*C,2\u4ef6", 16.9, 1),
            detail("1mm*1250mm*C,2\u4ef6", 16.9, 2),
        ]
        first_record = record("record-1", 16.9)
        second_record = record("record-2", 16.9)
        surplus_record = record("record-3", 16.9)

        issues, handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(),
                details,
                freight_data(first_record, second_record, surplus_record),
            )
        )

        self.assertEqual(issues, [])
        self.assertEqual(handled, {"record-1", "record-2", "record-3"})
        self.assertEqual(len(details), 2)
        self.assertIs(details[0].freight_record, first_record)
        self.assertIs(details[1].freight_record, second_record)

    def test_same_values_on_different_dates_are_not_collapsed(self) -> None:
        details = [detail("1mm*1250mm*C,1\u4ef6", 34.2, 1)]
        same_day_record = record("record-1", 34.2)
        other_day_record = record(
            "record-2",
            34.2,
            outbound_datetime="2026-08-24 02:14:05",
        )

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(same_day_record, other_day_record)
            )
        )

        self.assertEqual(len(details), 2)
        self.assertIs(details[0].freight_record, same_day_record)
        self.assertIs(details[1].freight_record, other_day_record)
        self.assertTrue(
            any("\u8bb0\u5f55\u672a\u5339\u914d\u5230PDF\u89c4\u683c" in issue for issue in issues)
        )

    def test_narrow_relative_weight_window_resolves_distinct_same_spec_rows(self) -> None:
        details = [
            detail("22mm*2500mm*12000mm,2\u4ef6", 10.362, 1),
            detail("22mm*2500mm*12000mm,3\u4ef6", 15.543, 2),
        ]
        lighter_record = record(
            "record-1", 10.312, raw_spec="22mm*2500mm*12000mm"
        )
        heavier_record = record(
            "record-2", 15.468, raw_spec="22mm*2500mm*12000mm"
        )

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(heavier_record, lighter_record)
            )
        )

        self.assertEqual(issues, [])
        self.assertIs(details[0].freight_record, lighter_record)
        self.assertIs(details[1].freight_record, heavier_record)

    def test_non_equivalent_equal_count_candidates_do_not_use_order(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 30.0, 1),
            detail("1mm*1250mm*C,1件", 40.0, 2),
        ]
        first_record = record("record-1", 10.0)
        second_record = record("record-2", 20.0)

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(first_record, second_record)
            )
        )

        self.assertIsNone(details[0].freight_record)
        self.assertIsNone(details[1].freight_record)
        self.assertEqual(sum("规格匹配歧义" in issue for issue in issues), 2)

    def test_weight_match_on_different_date_does_not_guess(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 24.67, 1, "2026-08-24 02:39:38"),
            detail("1mm*1250mm*C,1件", 8.39, 2, "2026-08-24 02:39:38"),
        ]
        freight_record = record("record-1", 24.67, "2026-08-23 02:14:05")

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(freight_record)
            )
        )

        self.assertIsNone(details[0].freight_record)
        self.assertIsNone(details[1].freight_record)
        self.assertEqual(sum("规格匹配歧义" in issue for issue in issues), 2)

    def test_weight_match_allows_narrow_cross_midnight_window(self) -> None:
        details = [
            detail("1mm*1250mm*C,3件", 24.67, 1, "2026-08-24 00:05:00"),
            detail("1mm*1250mm*C,1件", 8.39, 2, "2026-08-24 00:05:00"),
        ]
        freight_record = record("record-1", 24.67, "2026-08-23 23:58:00")

        issues, _handled, _applicable, _pending = (
            result_check.match_freight_to_sales_details(
                task(), details, freight_data(freight_record)
            )
        )

        self.assertIs(details[0].freight_record, freight_record)
        self.assertIsNone(details[1].freight_record)
        self.assertFalse(any("规格匹配歧义" in issue for issue in issues))


class FreightIdentityTest(unittest.TestCase):
    def test_legacy_first_record_key_and_duplicate_occurrence_are_distinct(self) -> None:
        payload = {
            "sheet": SHEET,
            "source_kind": SOURCE_KIND,
            "source_value": SOURCE_VALUE,
            "waybill": WAYBILL,
            "car": "冀Z00005",
            "outbound_datetime": "2026-08-23 02:14:05",
            "raw_spec": "1mm*1250mm*C",
            "settlement_quantity": 16.9,
            "unit_price": 75.0,
        }
        legacy_key = freight_record_key(payload)
        self.assertEqual(legacy_key, freight_record_key({**payload, "record_occurrence": 1}))
        self.assertNotEqual(
            legacy_key,
            freight_record_key({**payload, "record_occurrence": 2}),
        )

    def test_visible_duplicate_cards_receive_separate_instance_keys(self) -> None:
        snapshots = annotate_card_occurrences(
            [{"card_key": "same"}, {"card_key": "same"}, {"card_key": "other"}]
        )
        self.assertEqual([item["card_occurrence"] for item in snapshots], [1, 2, 1])
        self.assertEqual(
            [item["instance_key"] for item in snapshots],
            ["same:1", "same:2", "other:1"],
        )

    def test_physical_occurrence_survives_viewport_local_reset(self) -> None:
        first_view = annotate_card_occurrences([{"card_key": "same"}])
        second_view = annotate_card_occurrences([{"card_key": "same"}])
        self.assertEqual(first_view[0]["card_occurrence"], 1)
        self.assertEqual(second_view[0]["card_occurrence"], 1)

        encounter_counts: dict[str, int] = {}
        first = next_card_occurrence(encounter_counts, first_view[0]["card_key"])
        second = next_card_occurrence(encounter_counts, second_view[0]["card_key"])
        self.assertEqual((first, second), (1, 2))


if __name__ == "__main__":
    unittest.main()
