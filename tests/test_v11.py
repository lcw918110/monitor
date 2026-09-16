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
                            "net_rx_mbps": 20.0,
                            "net_tx_mbps": 5.0,
                            "net_rated_mbps": 1000,
                            "net_link_mbps": 1000,
                            "net_rx_percent": 2.0,
                            "net_tx_percent": 0.5,
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
        self.assertEqual(pstats["metrics"]["net_rated_mbps"]["avg"], 1000)
        self.assertEqual(pstats["metrics"]["net_rx_mbps"]["avg"], 20.0)
        self.assertEqual(pstats["metrics"]["net_rx_percent"]["avg"], 2.0)

        _, hosts = api_mod.handle_hosts_list(self.storage)
        self.assertEqual(hosts["hosts"][0]["net_rated_mbps"], 1000)
        self.assertEqual(hosts["hosts"][0]["net_rx_mbps"], 20.0)

        _, detail = api_mod.handle_host_detail(self.storage, "h1")
        sysinfo = (detail.get("payload") or {}).get("system") or {}
        self.assertEqual(sysinfo.get("net_rated_mbps"), 1000)
        self.assertEqual(sysinfo.get("net_rx_percent"), 2.0)

        pts = hist["points"]
        self.assertTrue(any(p.get("net_rated_mbps") == 1000 for p in pts))

        code, csv_text = api_mod.handle_export_csv(self.storage)
        self.assertEqual(code, 200)
        self.assertIn("host_id", csv_text)
        self.assertIn("npu_count", csv_text)
        self.assertIn("net_rated_mbps", csv_text)


if __name__ == "__main__":
    unittest.main()
