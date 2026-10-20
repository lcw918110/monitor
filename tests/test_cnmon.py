"""寒武纪 cnmon info：一张物理卡只出一条记录，字段来自对应小节。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.accelerators import (  # noqa: E402
    _parse_cnmon_info,
    _run,
    collect_accelerators,
)
from center import api as api_mod  # noqa: E402
from center.anomaly import judge_host_payload  # noqa: E402
from center.storage import Storage  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "accel", "cambricon")

# 数值直接来自三台机器的 cnmon info 原文（序列号已换成占位符）。
EXPECTED = {
    "172.24.5.49": {
        "index": 0,
        "name": "MLU370-S4",
        "bus_id": "0000:86:00.0",
        "util_percent": 18.0,
        "temp_c": 74.0,
        "mem_used_mb": 445.0,
        "mem_total_mb": 23365.0,
        "mem_percent": round(445.0 * 100.0 / 23365.0, 2),
        "power_w": 42.0,
        "health": "Good",
        "tool_version": "5.10.13",
    },
    "192.168.198.108": {
        "index": 0,
        "name": "MLU370-S4",
        "bus_id": "0000:01:00.0",
        "util_percent": 0.0,
        "temp_c": 73.0,
        "mem_used_mb": 0.0,
        "mem_total_mb": 23365.0,
        "mem_percent": 0.0,
        "power_w": 28.0,
        "health": "Good",
        "tool_version": "5.10.13",
    },
    "192.168.198.107": {
        "index": 0,
        "name": "MLU270-F4",
        "bus_id": "0000:02:00.0",
        "util_percent": 0.0,
        "temp_c": 46.0,
        "mem_used_mb": 4021.0,
        "mem_total_mb": 16384.0,
        "mem_percent": round(4021.0 * 100.0 / 16384.0, 2),
        "power_w": 23.0,
        "health": None,
        "tool_version": "1.19.5",
    },
}


def _read(ip: str) -> str:
    path = os.path.join(FIXTURES, ip, "raw", "cnmon_info.txt")
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _health_codes(card: dict) -> list:
    judged = judge_host_payload(
        {
            "accelerators": [card],
            "npus": [card],
            "gpus": [],
            "system": {"cpu_percent": 10, "mem_percent": 20, "disk_percent": 30},
        }
    )
    return [item.get("code") for item in judged.get("findings") or []]


class CnmonParseTests(unittest.TestCase):
    def test_each_fixture_is_one_card(self):
        for name, expect in EXPECTED.items():
            text = _read(name)
            self.assertIn("MLU 0-3", text, name)
            self.assertIn("Virtual Memory Usage", text, name)
            cards = _parse_cnmon_info(text)
            self.assertEqual(len(cards), 1, name)
            card = cards[0]
            for key, value in expect.items():
                self.assertEqual(card.get(key), value, "%s %s" % (name, key))
            self.assertEqual(card["vendor"], "cambricon")
            self.assertNotEqual(card["mem_used_mb"], 10543.0)
            self.assertNotEqual(card["health"], "State")

    def test_summary_table_without_card_header_is_not_split(self):
        table = "\n".join(
            [
                "| Card  VF  Name       Firmware | Bus-Id | Util |",
                "| 0     /   MLU370-S4    v1.1.6 | 0000:86:00.0 | 18% |",
                "| MLU 0-3                    : 37 % |",
            ]
        )
        self.assertEqual(_parse_cnmon_info(table), [])

    def test_cluster_temperature_when_chip_and_board_missing(self):
        text = "\n".join(
            [
                "Card 2",
                "    Product Name                   : MLU-CLUSTER",
                "    Temperature                    :",
                "        Cluster 0                  : 40 C",
                "        Cluster 1                  : 51 C",
                "        Cluster 3                  : 44 C",
                "    Physical Memory Usage          :",
                "        Total                      : 1 GiB",
                "        Used                       : 512 MiB",
                "    Power                          :",
                "        Usage                      : 7.5 W",
            ]
        )
        cards = _parse_cnmon_info(text)
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["index"], 2)
        self.assertEqual(card["name"], "MLU-CLUSTER")
        self.assertEqual(card["temp_c"], 51.0)
        self.assertEqual(card["mem_total_mb"], 1024.0)
        self.assertEqual(card["mem_used_mb"], 512.0)
        self.assertEqual(card["mem_percent"], 50.0)
        self.assertEqual(card["power_w"], 7.5)
        self.assertIsNone(card["health"])
        self.assertIsNone(card["util_percent"])

    def test_two_card_headers_stay_two_cards(self):
        text = "\n".join(
            [
                "Card 0",
                "    Product Name                   : MLU370-S4",
                "        MLU 0-3                    : 1 %",
                "Card 1",
                "    Product Name                   : MLU370-S4",
                "    Utilization                    :",
                "        MLU Average                : 3 %",
            ]
        )
        cards = _parse_cnmon_info(text)
        self.assertEqual([c["index"] for c in cards], [0, 1])
        self.assertEqual(cards[1]["util_percent"], 3.0)
        self.assertIsNone(cards[0]["util_percent"])

    def test_good_is_not_a_health_warning_and_state_still_is(self):
        good = _parse_cnmon_info(_read("172.24.5.49"))[0]
        self.assertNotIn("accel_health", _health_codes(good))
        bare = _parse_cnmon_info(_read("192.168.198.107"))[0]
        self.assertIsNone(bare["health"])
        self.assertNotIn("accel_health", _health_codes(bare))
        stale = dict(good)
        stale["health"] = "State"
        self.assertIn("accel_health", _health_codes(stale))

    def test_run_keeps_output_when_cnmon_exits_nonzero(self):
        sample = "Card 0\n    Product Name                   : MLU370-S4\n"
        proc = subprocess.CompletedProcess(
            ["cnmon", "info"], 1, stdout=sample, stderr="driver mismatch\n"
        )
        with mock.patch("subprocess.run", return_value=proc):
            text = _run(["cnmon", "info"])
        self.assertIn(sample, text)
        self.assertIn("driver mismatch", text)
        cards = _parse_cnmon_info(text)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "MLU370-S4")

    def test_cnmon_card_is_not_duplicated_by_pci(self):
        text = _read("192.168.198.107")
        root = tempfile.mkdtemp(prefix="cnmon-pci-")
        self.addCleanup(shutil.rmtree, root, True)
        slot = os.path.join(root, "0000:02:00.0")
        os.makedirs(slot)
        for name, value in (
            ("vendor", "0xcabc\n"),
            ("device", "0x0270\n"),
            ("class", "0x120000\n"),
            ("revision", "0x01\n"),
        ):
            with open(os.path.join(slot, name), "w", encoding="utf-8") as fh:
                fh.write(value)

        def which(cmd):
            if cmd == "cnmon":
                return "/usr/bin/cnmon"
            return None

        with mock.patch.dict(os.environ, {"MONITOR_SYSFS_PCI": root}):
            with mock.patch("agent.metrics.accelerators._run", return_value=text):
                with mock.patch("agent.metrics.accelerators.shutil.which", side_effect=which):
                    with mock.patch("agent.metrics.accelerators._from_nvidia", return_value=[]):
                        with mock.patch("agent.metrics.accelerators._from_amd", return_value=[]):
                            with mock.patch(
                                "agent.metrics.accelerators._from_huawei", return_value=[]
                            ):
                                with mock.patch(
                                    "agent.metrics.accelerators._from_rockchip_rknn",
                                    return_value=[],
                                ):
                                    cards = collect_accelerators()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "MLU270-F4")
        self.assertEqual(cards[0]["health"], None)
        self.assertNotEqual(cards[0].get("source"), "pci")

    def test_next_report_replaces_stale_card_inventory(self):
        tmp = tempfile.mkdtemp(prefix="cnmon-db-")
        self.addCleanup(shutil.rmtree, tmp, True)
        storage = Storage(os.path.join(tmp, "t.db"), offline_seconds=90, retention_days=7)
        now = int(time.time())
        stale = []
        for idx in (0, 4, 8, 12, 16, 20, 0):
            stale.append(
                {
                    "vendor": "cambricon",
                    "vendor_label": "寒武纪",
                    "index": idx,
                    "name": "Cambricon MLU" if idx else "MLU370-S4",
                    "health": "State" if idx == 0 else None,
                    "util_percent": None,
                }
            )
        fresh = _parse_cnmon_info(_read("172.24.5.49"))
        for offset, cards in ((0, stale), (1, fresh)):
            code, _body = api_mod.handle_metrics_post(
                storage,
                json.dumps(
                    {
                        "host_id": "cnmon-host",
                        "hostname": "cnmon-host",
                        "host_type": "gpu",
                        "timestamp": now + offset,
                        "system": {"cpu_percent": 10, "mem_percent": 20, "disk_percent": 30},
                        "accelerators": cards,
                        "npus": cards,
                        "gpus": [],
                    }
                ).encode("utf-8"),
            )
            self.assertEqual(code, 200)
        host = storage.get_host("cnmon-host")
        self.assertIsNotNone(host)
        self.assertEqual(host["accel_count"], 1)
        self.assertEqual(len(host["accel_inventory"]), 1)
        self.assertEqual(host["accel_inventory"][0]["count"], 1)
        self.assertEqual(host["accel_inventory"][0]["model"], "MLU370-S4")
        self.assertEqual(len(host["payload"]["accelerators"]), 1)
        self.assertEqual(host["payload"]["accelerators"][0]["health"], "Good")
        self.assertNotIn("accel_health", _health_codes(host["payload"]["accelerators"][0]))


if __name__ == "__main__":
    unittest.main()
