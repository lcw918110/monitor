#!/usr/bin/env python3
"""v1.2：NPU 解析、异常判定、统计。"""

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

from agent.metrics.npu import _parse_info_table
from center import api as api_mod
from center.anomaly import judge_host_payload
from center.storage import Storage


SAMPLE_NPU_INFO = """
+------------------------------------------------------------------------------+
| npu-smi 23.0.0                       Version: 23.0.0                         |
+-------------------+-----------------+----------------------------------------+
| NPU     Name      | Health          | Power(W)          Temp(C)              |
| Chip    Device    | Bus-Id          | AICore(%)         Memory-Usage(MB)     |
+===================+=================+========================================+
| 0       910B      | OK              | 12.8              49                   |
| 0       0         | 0000:05:00.0    | 12                2539 / 8192          |
+===================+=================+========================================+
| 1       910B      | Warning         | 30.1              88                   |
| 0       1         | 0000:06:00.0    | 96                7000 / 8192          |
+===================+=================+========================================+
"""


class NpuParseTests(unittest.TestCase):
    def test_parse_table(self) -> None:
        npus = _parse_info_table(SAMPLE_NPU_INFO)
        self.assertEqual(len(npus), 2)
        self.assertEqual(npus[0]["index"], 0)
        self.assertEqual(npus[0]["health"], "OK")
        self.assertEqual(npus[0]["util_percent"], 12)
        self.assertEqual(npus[0]["mem_used_mb"], 2539)
        self.assertEqual(npus[0]["mem_total_mb"], 8192)
        self.assertEqual(npus[1]["health"], "Warning")
        self.assertEqual(npus[1]["util_percent"], 96)


class AnomalyTests(unittest.TestCase):
    def test_cpu_npu_judge(self) -> None:
        payload = {
            "system": {"cpu_percent": 96, "cpu_count": 8, "load1": 1.0},
            "npus": [
                {
                    "index": 0,
                    "health": "OK",
                    "util_percent": 10,
                    "temp_c": 50,
                    "mem_percent": 20,
                },
                {
                    "index": 1,
                    "health": "Warning",
                    "util_percent": 97,
                    "temp_c": 96,
                    "mem_percent": 92,
                },
            ],
        }
        result = judge_host_payload(payload, online=True)
        self.assertEqual(result["cpu"]["status"], "critical")
        self.assertIn(result["npu"]["status"], ("warn", "critical"))
        self.assertEqual(result["overall"], "critical")


class ApiV12Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "t.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_npu_ingest_and_anomaly_api(self) -> None:
        payload = {
            "host_id": "n1",
            "hostname": "n1",
            "host_type": "auto",
            "timestamp": int(time.time()),
            "system": {
                "cpu_percent": 20,
                "cpu_count": 16,
                "mem_percent": 40,
                "disk_percent": 20,
                "load1": 1,
                "uptime_sec": 10,
            },
            "npus": [
                {
                    "index": 0,
                    "name": "910B",
                    "health": "OK",
                    "util_percent": 30,
                    "temp_c": 50,
                    "mem_used_mb": 1000,
                    "mem_total_mb": 8000,
                    "mem_percent": 12.5,
                }
            ],
            "gpus": [],
        }
        code, resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(payload).encode("utf-8")
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])

        code, hosts = api_mod.handle_hosts_list(self.storage)
        self.assertEqual(hosts["hosts"][0]["host_type"], "gpu")
        self.assertEqual(hosts["hosts"][0]["npu_count"], 1)

        code, stats = api_mod.handle_stats(self.storage)
        self.assertEqual(stats["npu_cards"], 1)

        code, anomaly = api_mod.handle_anomaly(self.storage)
        self.assertEqual(anomaly["summary"]["normal"], 1)


if __name__ == "__main__":
    unittest.main()
