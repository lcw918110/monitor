#!/usr/bin/env python3
"""历史接口基础测试（告警相关已移除，保留 history/export）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center.storage import Storage


class HistoryExportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=7,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_history_and_export(self) -> None:
        now = int(time.time())
        for i in range(5):
            api_mod.handle_metrics_post(
                self.storage,
                json.dumps(
                    {
                        "host_id": "h1",
                        "hostname": "h1",
                        "host_type": "app",
                        "timestamp": now - (5 - i) * 30,
                        "system": {
                            "cpu_percent": 40,
                            "cpu_count": 8,
                            "mem_percent": 40,
                            "disk_percent": 20,
                            "load1": 1,
                            "uptime_sec": 10,
                        },
                        "npus": [],
                        "gpus": [],
                    }
                ).encode("utf-8"),
            )

        code, hist = api_mod.handle_host_history(self.storage, "h1", "minutes=60&limit=100")
        self.assertEqual(code, 200)
        self.assertGreaterEqual(hist["count"], 5)

        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "minutes=60"
        )
        self.assertEqual(code, 200)
        self.assertTrue(pstats["ok"])
        self.assertGreaterEqual(pstats["sample_count"], 5)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["avg"], 40)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["max"], 40)

        code, csv_text = api_mod.handle_export_csv(self.storage)
        self.assertEqual(code, 200)
        self.assertIn("host_id", csv_text)
        self.assertIn("npu_count", csv_text)


if __name__ == "__main__":
    unittest.main()
