#!/usr/bin/env python3
"""AMD rocm-smi / amd-smi 解析、库存摘要、host_type=auto。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.accelerators import collect_accelerators, split_for_payload
from agent.metrics.amd import (
    collect_amd_gpus,
    parse_amd_smi_json,
    parse_rocm_smi_csv,
    parse_rocm_smi_json,
    parse_rocm_smi_text,
)
from agent.metrics.gpu import collect_gpus, parse_nvidia_smi_csv
from center import api as api_mod
from center.storage import Storage
from common.accel_identity import shorten_card_model, summarize_accelerators
from common.host_type import resolve_auto_host_type

FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")
ROCM_JSON = os.path.join(FIXTURE_DIR, "rocm_smi_navi21_x5.json")
ROCM_CSV = os.path.join(FIXTURE_DIR, "rocm_smi_navi21.csv")
AMD_METRIC = os.path.join(FIXTURE_DIR, "amd_smi_metric.json")
AMD_STATIC = os.path.join(FIXTURE_DIR, "amd_smi_static.json")

ROCM_TEXT = """
GPU[0]          : GPU use (%): 8
GPU[0]          : VRAM Total Memory (B): 17163091968
GPU[0]          : VRAM Total Used Memory (B): 858154496
GPU[0]          : Temperature (Sensor edge) (C): 42.0
GPU[0]          : Average Graphics Package Power (W): 28.5
GPU[0]          : Card series:     Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]
GPU[1]          : GPU use (%): 12
GPU[1]          : VRAM Total Memory (B): 17163091968
GPU[1]          : VRAM Total Used Memory (B): 1024458752
GPU[1]          : Temperature (Sensor edge) (C): 45.0
GPU[1]          : Card series:     Navi 21 [Radeon RX 6800/6800 XT / 6900 XT]
"""


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class RocmSmiParseTests(unittest.TestCase):
    def test_json_five_navi21(self) -> None:
        cards = parse_rocm_smi_json(_read(ROCM_JSON))
        self.assertEqual(len(cards), 5)
        self.assertEqual([c["index"] for c in cards], [0, 1, 2, 3, 4])
        self.assertEqual(cards[0]["vendor"], "amd")
        self.assertEqual(cards[0]["card_kind"], "gpu")
        self.assertEqual(cards[0]["util_percent"], 8.0)
        self.assertAlmostEqual(cards[0]["mem_total_mb"], 17163091968 / (1024.0 * 1024.0), places=2)
        self.assertAlmostEqual(cards[0]["mem_used_mb"], 858154496 / (1024.0 * 1024.0), places=2)
        self.assertEqual(cards[0]["temp_c"], 42.0)
        self.assertEqual(cards[0]["power_w"], 28.5)
        self.assertEqual(cards[0]["power_limit_w"], 250.0)
        self.assertIn("Navi 21", cards[0]["name"])
        self.assertEqual(cards[3]["util_percent"], 55.0)
        self.assertIsNone(cards[0].get("note"))

    def test_csv_with_comma_in_vendor(self) -> None:
        cards = parse_rocm_smi_csv(_read(ROCM_CSV))
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[1]["util_percent"], 12.0)
        self.assertIn("Navi 21", cards[0]["name"])

    def test_gpu_bracket_text(self) -> None:
        cards = parse_rocm_smi_text(ROCM_TEXT)
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["index"], 0)
        self.assertEqual(cards[0]["util_percent"], 8.0)
        self.assertEqual(cards[1]["temp_c"], 45.0)


class AmdSmiParseTests(unittest.TestCase):
    def test_metric_and_static(self) -> None:
        cards = parse_amd_smi_json(_read(AMD_METRIC), _read(AMD_STATIC))
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["index"], 0)
        self.assertEqual(cards[0]["name"], "Radeon RX 6800")
        self.assertEqual(cards[0]["util_percent"], 15.0)
        self.assertEqual(cards[0]["mem_total_mb"], 16384.0)
        self.assertEqual(cards[0]["mem_used_mb"], 1200.0)
        self.assertEqual(cards[0]["temp_c"], 48.0)
        self.assertEqual(cards[1]["util_percent"], 90.0)
        self.assertEqual(cards[0]["source"], "amd-smi")


class CollectAmdPreferenceTests(unittest.TestCase):
    def test_prefers_rocm_smi(self) -> None:
        def fake_which(name: str):
            if name in ("rocm-smi", "amd-smi"):
                return "/usr/bin/" + name
            return None

        calls = []

        def fake_run(cmd, timeout=8.0):
            calls.append(list(cmd))
            if cmd and cmd[0] == "rocm-smi" and "--json" in cmd:
                return _read(ROCM_JSON)
            if cmd and cmd[0] == "amd-smi":
                self.fail("不应在 rocm-smi 成功时调用 amd-smi")
            return ""

        with mock.patch("agent.metrics.amd.shutil.which", side_effect=fake_which):
            with mock.patch("agent.metrics.amd._run", side_effect=fake_run):
                cards = collect_amd_gpus()
        self.assertEqual(len(cards), 5)
        self.assertTrue(any(c[0] == "rocm-smi" for c in calls))

    def test_fallback_amd_smi(self) -> None:
        def fake_which(name: str):
            if name in ("rocm-smi", "amd-smi"):
                return "/usr/bin/" + name
            return None

        def fake_run(cmd, timeout=8.0):
            if cmd and cmd[0] == "rocm-smi":
                return "ERROR: No AMD GPUs specified"
            if cmd == ["amd-smi", "metric", "--json"]:
                return _read(AMD_METRIC)
            if cmd == ["amd-smi", "static", "--json"]:
                return _read(AMD_STATIC)
            return ""

        with mock.patch("agent.metrics.amd.shutil.which", side_effect=fake_which):
            with mock.patch("agent.metrics.amd._run", side_effect=fake_run):
                cards = collect_amd_gpus()
        self.assertEqual(len(cards), 2)
        self.assertEqual(cards[0]["source"], "amd-smi")

    def test_no_tools_empty(self) -> None:
        with mock.patch("agent.metrics.amd.shutil.which", return_value=None):
            self.assertEqual(collect_amd_gpus(), [])


class AcceleratorWireTests(unittest.TestCase):
    def test_collect_accelerators_includes_amd(self) -> None:
        def fake_which(name: str):
            return "/usr/bin/rocm-smi" if name == "rocm-smi" else None

        def fake_run(cmd, timeout=8.0):
            if cmd and cmd[0] == "rocm-smi" and "--json" in cmd:
                return _read(ROCM_JSON)
            return ""

        with mock.patch("agent.metrics.amd.shutil.which", side_effect=fake_which):
            with mock.patch("agent.metrics.amd._run", side_effect=fake_run):
                with mock.patch("agent.metrics.accelerators._from_nvidia", return_value=[]):
                    with mock.patch("agent.metrics.accelerators._from_huawei", return_value=[]):
                        with mock.patch(
                            "agent.metrics.accelerators._from_cambricon", return_value=[]
                        ):
                            with mock.patch(
                                "agent.metrics.accelerators._from_rockchip_rknn",
                                return_value=[],
                            ):
                                cards = collect_accelerators()
        self.assertEqual(len(cards), 5)
        self.assertTrue(all(c["vendor"] == "amd" for c in cards))
        parts = split_for_payload(cards)
        self.assertEqual(len(parts["gpus"]), 5)
        self.assertEqual(parts["npus"], [])
        self.assertEqual(len(parts["accelerators"]), 5)

    def test_nvidia_still_in_gpus(self) -> None:
        nvidia = {
            "vendor": "nvidia",
            "vendor_label": "英伟达",
            "card_kind": "gpu",
            "index": 0,
            "name": "NVIDIA GeForce RTX 3090",
        }
        huawei = {
            "vendor": "huawei",
            "card_kind": "npu",
            "index": 0,
            "name": "910B",
        }
        parts = split_for_payload([nvidia, huawei])
        self.assertEqual(parts["gpus"], [nvidia])
        self.assertEqual(parts["npus"], [huawei])

    def test_nvidia_smi_failure_empty(self) -> None:
        with mock.patch("agent.metrics.gpu.shutil.which", return_value="/usr/bin/nvidia-smi"):
            with mock.patch("agent.metrics.gpu._run_nvidia_query", return_value=None):
                self.assertEqual(collect_gpus(), [])

    def test_nvidia_csv_regression(self) -> None:
        text = "0, GPU-abc, Tesla T4, 12, 15360, 1024, 45, 30.50, 70.00, 40"
        gpus = parse_nvidia_smi_csv(text, has_power_limit=True)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["util_percent"], 12.0)


class IdentityTests(unittest.TestCase):
    def test_amd_navi21_summary(self) -> None:
        cards = parse_rocm_smi_json(_read(ROCM_JSON))
        ident = summarize_accelerators(cards)
        self.assertEqual(ident["count"], 5)
        self.assertEqual(ident["summary"], "AMD ×5 Radeon RX 6800/6900")
        self.assertEqual(ident["inventory"][0]["count"], 5)

    def test_nvidia_rtx3090_summary(self) -> None:
        cards = [
            {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090"},
            {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090"},
        ]
        ident = summarize_accelerators(cards)
        self.assertEqual(ident["summary"], "NVIDIA ×2 RTX 3090")
        self.assertEqual(shorten_card_model("NVIDIA GeForce RTX 3090"), "RTX 3090")

    def test_mixed_vendors(self) -> None:
        cards = parse_rocm_smi_json(_read(ROCM_JSON))
        cards.extend(
            [
                {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090"},
                {"vendor": "nvidia", "name": "NVIDIA GeForce RTX 3090"},
            ]
        )
        ident = summarize_accelerators(cards)
        self.assertEqual(
            ident["summary"], "AMD ×5 Radeon RX 6800/6900 · NVIDIA ×2 RTX 3090"
        )

    def test_auto_host_type(self) -> None:
        self.assertEqual(resolve_auto_host_type(True), "gpu")
        self.assertEqual(resolve_auto_host_type(False), "cpu")


class CenterIdentityApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "t.db"))
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _amd_payload(self, host_id: str = "amd-01") -> dict:
        cards = parse_rocm_smi_json(_read(ROCM_JSON))
        parts = split_for_payload(cards)
        return {
            "host_id": host_id,
            "hostname": host_id,
            "host_type": "auto",
            "timestamp": self.now,
            "system": {
                "cpu_percent": 10,
                "cpu_count": 16,
                "mem_percent": 40,
                "disk_percent": 20,
                "load1": 1.0,
            },
            "accelerators": parts["accelerators"],
            "gpus": parts["gpus"],
            "npus": parts["npus"],
        }

    def test_auto_becomes_gpu_and_list_exposes_identity(self) -> None:
        code, resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(self._amd_payload()).encode("utf-8")
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])
        _, hosts = api_mod.handle_hosts_list(self.storage)
        host = hosts["hosts"][0]
        self.assertEqual(host["host_type"], "gpu")
        self.assertEqual(host["gpu_count"], 5)
        self.assertEqual(host["accel_count"], 5)
        self.assertEqual(host["accel_summary"], "AMD ×5 Radeon RX 6800/6900")
        self.assertEqual(host["accel_inventory"][0]["model"], "Radeon RX 6800/6900")

        _, detail = api_mod.handle_host_detail(self.storage, "amd-01")
        self.assertEqual(detail["host_type"], "gpu")
        self.assertEqual(detail["accel_summary"], "AMD ×5 Radeon RX 6800/6900")
        self.assertEqual(len(detail["payload"]["accelerators"]), 5)

        _, stats = api_mod.handle_stats(self.storage)
        self.assertEqual(stats["gpu_cards"], 5)
        self.assertEqual(stats["accel_cards"], 5)
        self.assertEqual(stats["accel_summary"], "AMD ×5 Radeon RX 6800/6900")

    def test_old_nvidia_payload_still_summarizes(self) -> None:
        payload = {
            "host_id": "nv-01",
            "hostname": "nv-01",
            "host_type": "auto",
            "timestamp": self.now,
            "system": {"cpu_percent": 8, "mem_percent": 30, "disk_percent": 10},
            "gpus": [
                {
                    "index": 0,
                    "name": "NVIDIA GeForce RTX 3090",
                    "util_percent": 40,
                },
                {
                    "index": 1,
                    "name": "NVIDIA GeForce RTX 3090",
                    "util_percent": 10,
                },
            ],
            "npus": [],
        }
        api_mod.handle_metrics_post(self.storage, json.dumps(payload).encode("utf-8"))
        _, hosts = api_mod.handle_hosts_list(self.storage)
        host = hosts["hosts"][0]
        self.assertEqual(host["host_type"], "gpu")
        self.assertEqual(host["gpu_count"], 2)
        self.assertEqual(host["accel_summary"], "NVIDIA ×2 RTX 3090")

    def test_period_stats_include_identity(self) -> None:
        payload = self._amd_payload()
        for i in range(3):
            payload["timestamp"] = self.now - (3 - i) * 30
            payload["accelerators"][0]["util_percent"] = 10 + i
            api_mod.handle_metrics_post(
                self.storage, json.dumps(payload).encode("utf-8")
            )
        code, data = api_mod.handle_cluster_period_stats(self.storage, "minutes=60")
        self.assertEqual(code, 200)
        self.assertEqual(data["accel_summary"], "AMD ×5 Radeon RX 6800/6900")
        self.assertEqual(data["hosts"][0]["accel_summary"], "AMD ×5 Radeon RX 6800/6900")
        self.assertEqual(data["hosts"][0]["accel_count"], 5)
        self.assertGreater(data["hosts"][0]["sample_count"], 0)
        code, csv_body = api_mod.handle_export_period_csv(self.storage, "minutes=60")
        self.assertEqual(code, 200)
        self.assertIn("accel_summary", csv_body.splitlines()[0])
        self.assertIn("AMD", csv_body)

    def test_manual_cpu_unchanged(self) -> None:
        payload = self._amd_payload("cpu-01")
        payload["host_type"] = "cpu"
        # 中心端不负责停采，只归一类型；手动 cpu 保持 cpu
        api_mod.handle_metrics_post(self.storage, json.dumps(payload).encode("utf-8"))
        _, hosts = api_mod.handle_hosts_list(self.storage)
        self.assertEqual(hosts["hosts"][0]["host_type"], "cpu")


if __name__ == "__main__":
    unittest.main()
