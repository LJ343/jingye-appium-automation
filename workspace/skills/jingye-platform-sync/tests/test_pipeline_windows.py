from __future__ import annotations

import argparse
import datetime as dt
import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / 'scripts'
sys.path.insert(0, str(SCRIPT_DIR))

import jingye_pipeline  # noqa: E402


class PipelineWindowsTest(unittest.TestCase):
    def test_sync_uses_frozen_anchor_and_includes_day_before_yesterday(self) -> None:
        anchor = dt.date(2026, 9, 8)
        args = argparse.Namespace(command='sync', anchor_date=anchor)

        windows = jingye_pipeline.resolve_windows(args)

        self.assertEqual(windows.mode, 'AUTO')
        self.assertEqual(windows.anchor, anchor)
        self.assertEqual(windows.purchase_start, dt.date(2026, 9, 6))
        self.assertEqual(windows.purchase_end, anchor)
        self.assertEqual(windows.sales_start, dt.date(2026, 9, 6))
        self.assertEqual(windows.sales_end, anchor)


if __name__ == '__main__':
    unittest.main()
