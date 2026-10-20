"""PCI sysfs 回退：没有厂商工具时仍能报出加速卡，且不产生 0% 利用率样本。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.accelerators import collect_accelerators, collect_pci_accelerators
from center import api as api_mod
from center.anomaly import judge_host_payload
from center.storage import Storage


def _write(root, bdf, vendor, device, class_id, revision):
    slot = os.path.join(root, bdf)
    os.makedirs(slot)
    with open(os.path.join(slot, "vendor"), "w", encoding="utf-8") as fh:
        fh.write(vendor + "\n")
    with open(os.path.join(slot, "device"), "w", encoding="utf-8") as fh:
        fh.write(device + "\n")
    with open(os.path.join(slot, "class"), "w", encoding="utf-8") as fh:
        fh.write(class_id + "\n")
    with open(os.path.join(slot, "revision"), "w", encoding="utf-8") as fh:
        fh.write(revision + "\n")


class PciAccelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pci-sysfs-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_cambricon_mlu270_rev_ff_has_no_util(self):
        _write(self.tmp, "0000:02:00.0", "0xcabc", "0x0270", "0x120000", "0xff")
        cards = collect_pci_accelerators(self.tmp)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["vendor"], "cambricon")
        self.assertEqual(card["name"], "MLU270")
        self.assertEqual(card["pci"], "cabc:0270")
        self.assertEqual(card["health"], "abnormal")
        self.assertEqual(card["note"], "设备无响应 (rev ff)")
        self.assertIsNone(card["util_percent"])
        self.assertIsNone(card["mem_percent"])
        self.assertIsNone(card["temp_c"])
        self.assertNotEqual(card["util_percent"], 0)
        self.assertEqual(card["source"], "pci")

    def test_known_vendors_and_ignores_other_classes(self):
        _write(self.tmp, "0000:03:00.0", "0x10de", "0x1b06", "0x030000", "0xa1")
        _write(self.tmp, "0000:04:00.0", "0x1002", "0x73bf", "0x030000", "0x00")
        _write(self.tmp, "0000:05:00.0", "0x1002", "0x1482", "0x020000", "0x00")
        _write(self.tmp, "0000:06:00.0", "0x19e5", "0xd100", "0x120000", "0x20")
        _write(self.tmp, "0000:07:00.0", "0x8086", "0x1234", "0x120000", "0x01")
        _write(self.tmp, "0000:08:00.0", "0x10de", "0x10fa", "0x040300", "0x00")
        cards = collect_pci_accelerators(self.tmp)
        by_vendor = {}
        for card in cards:
            by_vendor.setdefault(card["vendor"], []).append(card)
        self.assertEqual(set(by_vendor), {"nvidia", "amd", "huawei"})
        self.assertEqual(len(by_vendor["nvidia"]), 1)
        self.assertEqual(by_vendor["nvidia"][0]["note"], "驱动/管理工具缺失")
        self.assertIsNone(by_vendor["nvidia"][0]["util_percent"])
        self.assertEqual(by_vendor["amd"][0]["vendor_label"], "AMD")
        self.assertEqual(by_vendor["huawei"][0]["name"], "Ascend 310")
        self.assertNotIn("8086", [c["pci"] for c in cards])

    def test_skip_vendor_already_reported(self):
        _write(self.tmp, "0000:02:00.0", "0xcabc", "0x0270", "0x120000", "0xff")
        _write(self.tmp, "0000:03:00.0", "0x10de", "0x1b06", "0x030000", "0xa1")
        cards = collect_pci_accelerators(self.tmp, skip_vendors=["cambricon"])
        self.assertEqual([c["vendor"] for c in cards], ["nvidia"])

    def test_collect_does_not_double_report_tool_cards(self):
        _write(self.tmp, "0000:02:00.0", "0xcabc", "0x0270", "0x120000", "0xff")
        tool = {
            "vendor": "cambricon",
            "vendor_label": "寒武纪",
            "name": "MLU270-from-cnmon",
            "index": 0,
            "util_percent": 12.0,
            "health": "OK",
        }
        with mock.patch.dict(os.environ, {"MONITOR_SYSFS_PCI": self.tmp}):
            with mock.patch("agent.metrics.accelerators._from_nvidia", return_value=[]):
                with mock.patch("agent.metrics.accelerators._from_amd", return_value=[]):
                    with mock.patch("agent.metrics.accelerators._from_huawei", return_value=[]):
                        with mock.patch(
                            "agent.metrics.accelerators._from_rockchip_rknn", return_value=[]
                        ):
                            with mock.patch(
                                "agent.metrics.accelerators._from_cambricon",
                                return_value=[tool],
                            ):
                                with mock.patch(
                                    "agent.metrics.accelerators._vendor_tool_vendors",
                                    return_value=[],
                                ):
                                    cards = collect_accelerators()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "MLU270-from-cnmon")
        self.assertNotEqual(cards[0].get("source"), "pci")

    def test_collect_skips_pci_when_tool_exists_but_returns_nothing(self):
        _write(self.tmp, "0000:02:00.0", "0xcabc", "0x0270", "0x120000", "0xff")
        with mock.patch.dict(os.environ, {"MONITOR_SYSFS_PCI": self.tmp}):
            with mock.patch("agent.metrics.accelerators._from_nvidia", return_value=[]):
                with mock.patch("agent.metrics.accelerators._from_amd", return_value=[]):
                    with mock.patch("agent.metrics.accelerators._from_huawei", return_value=[]):
                        with mock.patch(
                            "agent.metrics.accelerators._from_rockchip_rknn", return_value=[]
                        ):
                            with mock.patch(
                                "agent.metrics.accelerators._from_cambricon", return_value=[]
                            ):
                                with mock.patch(
                                    "agent.metrics.accelerators._vendor_tool_vendors",
                                    return_value=["cambricon"],
                                ):
                                    cards = collect_accelerators()
        self.assertEqual(cards, [])

    def test_missing_tools_uses_pci(self):
        _write(self.tmp, "0000:02:00.0", "0xcabc", "0x0270", "0x120000", "0xff")
        with mock.patch.dict(os.environ, {"MONITOR_SYSFS_PCI": self.tmp}):
            with mock.patch("agent.metrics.accelerators._from_nvidia", return_value=[]):
                with mock.patch("agent.metrics.accelerators._from_amd", return_value=[]):
                    with mock.patch("agent.metrics.accelerators._from_huawei", return_value=[]):
                        with mock.patch(
                            "agent.metrics.accelerators._from_rockchip_rknn", return_value=[]
                        ):
                            with mock.patch(
                                "agent.metrics.accelerators._from_cambricon", return_value=[]
                            ):
                                with mock.patch(
                                    "agent.metrics.accelerators._vendor_tool_vendors",
                                    return_value=[],
                                ):
                                    cards = collect_accelerators()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "MLU270")
        self.assertEqual(cards[0]["source"], "pci")
        self.assertIsNone(cards[0]["util_percent"])

    def test_pci_card_is_not_a_zero_util_sample(self):
        tmp = tempfile.mkdtemp(prefix="pci-db-")
        self.addCleanup(shutil.rmtree, tmp, True)
        storage = Storage(os.path.join(tmp, "t.db"), offline_seconds=90, retention_days=7)
        now = int(time.time())
        card = {
            "index": 0,
            "vendor": "cambricon",
            "vendor_label": "寒武纪",
            "name": "MLU270",
            "util_percent": None,
            "health": "abnormal",
            "note": "设备无响应 (rev ff)",
        }
        code, _body = api_mod.handle_metrics_post(
            storage,
            json.dumps(
                {
                    "host_id": "mlu-host",
                    "hostname": "mlu-host",
                    "host_type": "gpu",
                    "timestamp": now,
                    "system": {"cpu_percent": 10, "mem_percent": 20, "disk_percent": 30},
                    "accelerators": [card],
                    "npus": [card],
                    "gpus": [],
                }
            ).encode("utf-8"),
        )
        self.assertEqual(code, 200)
        stats = storage.host_period_stats("mlu-host", minutes=60)
        self.assertEqual(stats["metrics"]["cpu_percent"]["sample_count"], 1)
        self.assertEqual(stats["metrics"]["accel_util_avg"]["sample_count"], 0)
        self.assertIsNone(stats["metrics"]["accel_util_avg"]["avg"])
        self.assertEqual(stats["accel_count"], 1)
        self.assertIn("MLU270", stats["accel_summary"])
        judged = judge_host_payload(
            {
                "accelerators": [card],
                "npus": [card],
                "gpus": [],
                "system": {"cpu_percent": 10, "mem_percent": 20, "disk_percent": 30},
            }
        )
        self.assertEqual((judged.get("accel") or {}).get("status"), "warn")
        messages = " ".join(item.get("message") or "" for item in judged.get("findings") or [])
        self.assertIn("abnormal", messages)
        self.assertNotIn("0.0%", messages)
