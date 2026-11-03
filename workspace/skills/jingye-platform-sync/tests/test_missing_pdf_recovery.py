from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))

import jingye_pipeline  # noqa: E402
from jingye_store import (  # noqa: E402
    connect_db,
    iter_candidates,
    now_text,
    rebuild_logical_tickets,
    upsert_waybill,
)


class MissingPdfRecoveryTest(unittest.TestCase):
    def test_later_pdf_replaces_review_marker_and_restores_upload_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / 'facts.sqlite3'
            connection = connect_db(db_path)
            try:
                waybill_id = upsert_waybill(
                    connection,
                    'SALES',
                    'YS20260902111050606',
                    '冀Z00009',
                    cargo_name='冷轧卷',
                    destination='天津港一公司',
                    load_weight_kg=32860,
                    source_loading_point_name='河北敬业钢铁',
                )
                connection.commit()

                rebuild_logical_tickets(connection)
                first_ticket = connection.execute(
                    'SELECT validation_status, validation_message '
                    'FROM logical_tickets WHERE waybill_id=?',
                    (waybill_id,),
                ).fetchone()
                self.assertEqual(first_ticket['validation_status'], 'REVIEW_REQUIRED')
                self.assertEqual(first_ticket['validation_message'], '未找到PDF')
                first_candidate = next(
                    item
                    for item in iter_candidates(
                        connection,
                        'agent',
                        '敬业钢铁',
                        require_attachment=True,
                    )
                    if item.waybill_id == waybill_id
                )
                self.assertIsNone(first_candidate.payload)
                self.assertEqual(first_candidate.rejection_reason, '未找到PDF')
                self.assertEqual(
                    jingye_pipeline.read_upload_issue_details(db_path, '敬业钢铁'),
                    [
                        {
                            'direction': 'SALES',
                            'waybill_no': 'YS20260902111050606',
                            'plate_no': '冀Z00009',
                            'reason': '未找到PDF',
                        }
                    ],
                )

                current_time = now_text()
                sha256 = 'a' * 64
                connection.execute(
                    '''
                    INSERT INTO documents(
                        sha256, size_bytes, page_count, parser_version,
                        parse_status, pdf_business_code, pdf_plate_no,
                        pdf_goods_name, gross_weight_kg, tare_weight_kg,
                        net_weight_kg, gross_time, tare_time, net_time,
                        document_time, created_at, updated_at
                    ) VALUES (?, 2048, 1, 'test', 'VALID', ?, ?, ?,
                              40000, 7140, 32860, ?, ?, ?, ?, ?, ?)
                    ''',
                    (
                        sha256,
                        'YS20260902111050606',
                        '冀Z00009',
                        '冷轧卷',
                        '2026-09-02T13:39:37',
                        '2026-09-02T12:00:00',
                        '2026-09-02T13:39:37',
                        '2026-09-02T13:39:37',
                        current_time,
                        current_time,
                    ),
                )
                connection.execute(
                    '''
                    INSERT INTO waybill_documents(
                        waybill_id, sha256, attachment_index,
                        download_time, first_linked_at
                    ) VALUES (?, ?, 1, ?, ?)
                    ''',
                    (waybill_id, sha256, current_time, current_time),
                )
                connection.commit()

                rebuild_logical_tickets(connection)
                recovered_ticket = connection.execute(
                    'SELECT validation_status, validation_message '
                    'FROM logical_tickets WHERE waybill_id=?',
                    (waybill_id,),
                ).fetchone()
                self.assertEqual(recovered_ticket['validation_status'], 'VALID')
                self.assertEqual(recovered_ticket['validation_message'], '')

                # The normal attachment and OSS stages fill this field before upload.
                connection.execute(
                    'UPDATE logical_tickets SET oss_url=? WHERE waybill_id=?',
                    ('https://oss.example.test/YS20260902111050606.jpg', waybill_id),
                )
                connection.commit()
                recovered_candidate = next(
                    item
                    for item in iter_candidates(
                        connection,
                        'agent',
                        '敬业钢铁',
                        require_attachment=True,
                    )
                    if item.waybill_id == waybill_id
                )
                self.assertIsNotNone(recovered_candidate.payload)
                self.assertEqual(
                    recovered_candidate.payload['externalWaybillNo'],
                    'YS20260902111050606',
                )
                self.assertEqual(
                    jingye_pipeline.read_upload_issue_details(db_path, '敬业钢铁'),
                    [],
                )
            finally:
                connection.close()


if __name__ == '__main__':
    unittest.main()
