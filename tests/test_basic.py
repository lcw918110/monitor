#!/usr/bin/env python3
"""基础自测：存储、API、模拟 GPU 上报。不依赖第三方包。"""

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


class StorageApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=7)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _payload(self, host_id: str, with_gpu: bool = False) -> dict:
        data = {
            "host_id": host_id,
            "hostname": host_id,
            "host_type": "auto",
            "timestamp": int(time.time()),
            "system": {
                "cpu_percent": 11.5,
                "mem_total_mb": 16000,
                "mem_used_mb": 8000,
                "mem_percent": 50.0,
                "disk_total_gb": 500,
                "disk_used_gb": 100,
                "disk_percent": 20.0,
                "load1": 0.5,
                "uptime_sec": 1000,
            },
            "gpus": [],
        }
        if with_gpu:
            data["gpus"] = [
                {
                    "index": 0,
                    "name": "Fake-GPU",
                    "util_percent": 70,
                    "mem_total_mb": 24576,
                    "mem_used_mb": 10240,
                    "temp_c": 55,
                    "power_w": 120,
                }
            ]
        return data

    def test_app_and_gpu_ingest(self) -> None:
        code, resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(self._payload("app-1")).encode("utf-8")
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])

        code, resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(self._payload("gpu-1", with_gpu=True)).encode("utf-8")
        )
        self.assertEqual(code, 200)

        _, hosts = api_mod.handle_hosts_list(self.storage)
        self.assertEqual(len(hosts["hosts"]), 2)

        _, detail = api_mod.handle_host_detail(self.storage, "gpu-1")
        self.assertEqual(detail["host_type"], "gpu")
        self.assertEqual(len(detail["payload"]["gpus"]), 1)

        _, stats = api_mod.handle_stats(self.storage)
        self.assertEqual(stats["host_total"], 2)
        self.assertEqual(stats["host_online"], 2)
        self.assertEqual(stats["gpu_cards"], 1)
        self.assertIsNotNone(stats["avg_cpu_percent"])

    def test_token_required(self) -> None:
        body = json.dumps(self._payload("x")).encode("utf-8")
        code, resp = api_mod.handle_metrics_post(self.storage, body, expected_token="secret")
        self.assertEqual(code, 401)

        payload = self._payload("x")
        payload["token"] = "secret"
        code, resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(payload).encode("utf-8"), expected_token="secret"
        )
        self.assertEqual(code, 200)

    def test_offline(self) -> None:
        payload = self._payload("old")
        payload["timestamp"] = int(time.time()) - 120
        api_mod.handle_metrics_post(self.storage, json.dumps(payload).encode("utf-8"))
        _, hosts = api_mod.handle_hosts_list(self.storage)
        self.assertFalse(hosts["hosts"][0]["online"])


if __name__ == "__main__":
    unittest.main()
