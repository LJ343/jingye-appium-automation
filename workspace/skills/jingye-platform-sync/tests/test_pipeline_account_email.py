from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))

import jingye_pipeline  # noqa: E402


def summary_with_accounts() -> dict:
    account_payload = [
        {
            'account': '10000000008',
            'status': 'SUCCESS',
            'stage': '完整流程',
            'attempts': 1,
        },
        {
            'account': '10000000007',
            'status': 'SUCCESS',
            'stage': '完整流程',
            'attempts': 2,
        },
        {
            'account': '10000000010',
            'status': 'FAILED',
            'stage': '销售单',
            'attempts': 3,
        },
    ]
    return {
        'run_id': 'test-run',
        'log_path': 'local-test.log',
        'status': 'FAILED',
        'windows': {
            'mode': 'backfill',
            'anchor_date': '2026-09-01',
            'purchase_start': '2026-08-25',
            'purchase_end': '2026-09-01',
            'sales_start': '2026-08-25',
            'sales_end': '2026-09-01',
        },
        'steps': [
            {
                'name': 'crawl',
                'return_code': 1,
                'elapsed_seconds': 12.3,
                'output_tail': [
                    'STATUS:YYB_ACCOUNT_SUMMARY|'
                    + json.dumps(account_payload, ensure_ascii=False),
                ],
            },
            {
                'name': 'refresh',
                'return_code': 0,
                'elapsed_seconds': 1.2,
                'output_tail': [],
            },
        ],
        'status_before': {},
        'status_after': {},
        'upload_validation': {},
        'error_message': 'RuntimeError: 步骤 crawl 失败，返回码 1',
    }


class PipelineAccountEmailTest(unittest.TestCase):
    def test_account_results_are_masked_before_summary_or_email_use(self) -> None:
        summary = summary_with_accounts()

        results = jingye_pipeline.extract_crawl_account_results(summary)

        self.assertEqual(
            [item['account'] for item in results],
            ['*******0008', '*******0007', '*******0010'],
        )
        serialized = json.dumps(results, ensure_ascii=False)
        self.assertNotIn('10000000008', serialized)
        self.assertNotIn('10000000007', serialized)
        self.assertNotIn('10000000010', serialized)

    def test_email_lists_successes_and_failures_with_stage_and_attempts(self) -> None:
        summary = summary_with_accounts()
        summary['account_results'] = (
            jingye_pipeline.extract_crawl_account_results(summary)
        )

        body = jingye_pipeline.build_email_body(summary)

        self.assertIn('本次共处理 3 个账号：2 个完成，1 个失败', body)
        self.assertIn('完成账号：*******0008、*******0007', body)
        self.assertIn('*******0010：在销售单阶段失败，共执行 3 次', body)
        self.assertNotIn('10000000008', body)
        self.assertNotIn('10000000007', body)
        self.assertNotIn('10000000010', body)

    def test_email_uses_plain_chinese_and_lists_missing_pdf_waybill(self) -> None:
        summary = summary_with_accounts()
        summary.update(
            {
                'status': 'PARTIAL',
                'upload_validation': {
                    'selected': 685,
                    'accepted': 684,
                    'rejected': 1,
                    'accepted_without_submission': 7,
                    'accepted_successful': 677,
                    'rejection_reasons': {'未找到PDF': 1},
                },
                'upload_issues': [
                    {
                        'direction': 'SALES',
                        'waybill_no': 'YS20260902111050606',
                        'plate_no': '冀Z00008',
                        'reason': '未找到PDF',
                    }
                ],
                'status_before': {
                    'waybills': 680,
                    'purchase_waybills': 680,
                    'sales_waybills': 0,
                    'unique_documents': 680,
                    'freight_records': 10,
                    'successful': 677,
                },
                'status_after': {
                    'waybills': 685,
                    'purchase_waybills': 680,
                    'sales_waybills': 5,
                    'unique_documents': 684,
                    'freight_records': 12,
                    'freight_complete': 8,
                    'freight_pending': 4,
                    'successful': 684,
                    'retryable': 0,
                    'rejected': 0,
                },
                'error_message': (
                    '本地待完善候选=1，本次新增 rejected=0，retryable=0'
                ),
            }
        )

        body = jingye_pipeline.build_email_body(summary)

        self.assertIn('本次结果：部分完成', body)
        self.assertIn(
            '共检查 685 条运单，其中 684 条资料完整，1 条资料仍需完善',
            body,
        )
        self.assertIn('本轮新增成功上传 7 条', body)
        self.assertIn(
            '销售单 YS20260902111050606，车辆 冀Z00008：未找到PDF',
            body,
        )
        self.assertIn('人工补录或定时任务取得并完整解析该 PDF 后', body)
        self.assertIn('更新同一运单', body)
        self.assertNotIn('selected:', body)
        self.assertNotIn('accepted_without_submission', body)
        self.assertNotIn('rejection_reasons', body)
        self.assertNotIn('本地待完善候选=', body)


if __name__ == '__main__':
    unittest.main()
