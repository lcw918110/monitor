#!/usr/bin/env python3
"""昇腾 npu-smi 解析：310P3 真机样本 + 旧版双卡表。"""

from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.npu import (
    _enrich_with_typed_queries,
    _parse_chip_mapping,
    _parse_info_table,
    _parse_temp_text,
    _parse_usages_text,
    _sanitize_parsed_metrics,
    collect_npus,
)

FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "npu_smi_310p3_24_1_rc2.txt"
)

SAMPLE_NPU_INFO_V23 = """
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

PROCESS_PIDS = {2129107, 1925564, 2129214, 1925742}


def _load_fixture_sections() -> dict:
    with open(FIXTURE_PATH, encoding="utf-8") as fh:
        text = fh.read()
    sections: dict = {}
    current = None
    buf: list = []
    for line in text.splitlines(keepends=True):
        if line.startswith("=== ") and line.strip().endswith(" ==="):
            if current is not None:
                sections[current] = "".join(buf)
            current = line.strip().strip("=").strip()
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "".join(buf)
    return sections


class ParseInfoTableV23Tests(unittest.TestCase):
    def test_two_910b_cards(self) -> None:
        npus = _parse_info_table(SAMPLE_NPU_INFO_V23)
        self.assertEqual(len(npus), 2)
        self.assertEqual(npus[0]["index"], 0)
        self.assertEqual(npus[0]["health"], "OK")
        self.assertEqual(npus[0]["util_percent"], 12)
        self.assertEqual(npus[0]["temp_c"], 49)
        self.assertEqual(npus[0]["power_w"], 12.8)
        self.assertEqual(npus[0]["mem_used_mb"], 2539)
        self.assertEqual(npus[0]["mem_total_mb"], 8192)
        self.assertEqual(npus[1]["health"], "Warning")
        self.assertEqual(npus[1]["util_percent"], 96)
        self.assertEqual(npus[1]["temp_c"], 88)


class Ascend310P3FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sections = _load_fixture_sections()
        cls.info = cls.sections["npu-smi info"]
        cls.mapping = cls.sections["npu-smi info -m"]
        cls.usages = cls.sections["npu-smi info -t usages -i 1"]
        cls.temp = cls.sections["npu-smi info -t temp -i 1"]

    def test_info_table_one_card_not_process_rows(self) -> None:
        npus = _parse_info_table(self.info)
        self.assertEqual(len(npus), 1, npus)
        card = npus[0]
        self.assertEqual(card["index"], 1)
        self.assertEqual(card["health"], "OK")
        # 列对齐：Power=NA、Temp=71、Hugepages=380/380，不能对调
        self.assertEqual(card["temp_c"], 71)
        self.assertNotEqual(card["temp_c"], 380)
        self.assertIsNone(card["power_w"])
        self.assertNotEqual(card["power_w"], 71)
        self.assertEqual(card["util_percent"], 25)
        self.assertTrue(0 <= card["util_percent"] <= 100)
        self.assertEqual(card["mem_used_mb"], 2962)
        self.assertEqual(card["mem_total_mb"], 21527)
        self.assertNotIn(card["util_percent"], PROCESS_PIDS)
        self.assertNotIn(card.get("chip_id"), PROCESS_PIDS)

    def test_chip_mapping_excludes_mcu(self) -> None:
        chips = _parse_chip_mapping(self.mapping)
        self.assertEqual(len(chips), 2)
        mcu = [c for c in chips if c["is_mcu"]]
        compute = [c for c in chips if not c["is_mcu"]]
        self.assertEqual(len(mcu), 1)
        self.assertEqual(mcu[0]["name"], "Mcu")
        self.assertEqual(len(compute), 1)
        self.assertEqual(compute[0]["npu_id"], 1)
        self.assertEqual(compute[0]["chip_id"], 0)
        self.assertIn("310P3", compute[0]["name"])

    def test_usages_and_temp_typed_queries(self) -> None:
        usages = _parse_usages_text(self.usages)
        self.assertEqual(usages["util_percent"], 36)
        self.assertTrue(0 <= usages["util_percent"] <= 100)
        self.assertEqual(usages["mem_percent"], 13)
        self.assertEqual(usages["mem_total_mb"], 21527)
        # Hugepages 100% 不得当成 AICore / DDR 占用
        self.assertNotEqual(usages["util_percent"], 100)
        self.assertEqual(_parse_temp_text(self.temp), 71)

    def _fake_npu_smi(self, args, timeout=10.0):
        if args == ["info"]:
            return self.info
        if args == ["info", "-m"]:
            return self.mapping
        if args[:3] == ["info", "-t", "usages"] and "-i" in args:
            return self.usages
        if args[:3] == ["info", "-t", "temp"] and "-i" in args:
            return self.temp
        return None

    def test_collect_npus_card_count_and_metrics(self) -> None:
        with mock.patch("agent.metrics.npu._run_npu_smi", side_effect=self._fake_npu_smi):
            npus = collect_npus()
        self.assertEqual(len(npus), 1, npus)
        card = npus[0]
        self.assertEqual(card["index"], 1)
        self.assertEqual(card["temp_c"], 71)
        self.assertIsNone(card["power_w"])
        self.assertNotEqual(card["power_w"], 71)
        self.assertNotEqual(card["temp_c"], 380)
        self.assertTrue(0 <= card["util_percent"] <= 100)
        self.assertEqual(card["mem_used_mb"], 2962)
        self.assertEqual(card["mem_total_mb"], 21527)
        self.assertNotIn(int(card["util_percent"]), PROCESS_PIDS)

    def test_collect_npus_mapping_fallback_ignores_mcu(self) -> None:
        def fake_run(args, timeout=10.0):
            if args == ["info"]:
                return "unparseable"
            return self._fake_npu_smi(args, timeout)

        with mock.patch("agent.metrics.npu._run_npu_smi", side_effect=fake_run):
            npus = collect_npus()
        self.assertEqual(len(npus), 1, npus)
        self.assertEqual(npus[0]["index"], 1)
        self.assertEqual(npus[0]["temp_c"], 71)
        self.assertEqual(npus[0]["util_percent"], 36)
        self.assertNotIn("Mcu", npus[0]["name"])
        self.assertNotIn("MCU", npus[0]["name"].upper())

    def test_typed_query_repairs_hugepage_temp_and_pid_util(self) -> None:
        def fake_run(args, timeout=10.0):
            if args == ["info"]:
                # 仍含进程表；解析器必须先丢掉幽灵卡，再对非法值走 typed 查询
                return self.info
            return self._fake_npu_smi(args, timeout)

        with mock.patch("agent.metrics.npu._run_npu_smi", side_effect=fake_run):
            npus = collect_npus()
        self.assertEqual(len(npus), 1)
        self.assertNotEqual(npus[0]["temp_c"], 380)
        self.assertIsNone(npus[0]["power_w"])
        self.assertLessEqual(npus[0]["util_percent"], 100)

    def test_live_api_column_swap_repaired_by_typed_temp(self) -> None:
        """center-95 线上对象：temp/power 对调 + 两个 PID 幽灵卡。"""
        live_cards = [
            {
                "index": 1,
                "chip_id": 0,
                "name": "310P3",
                "health": "OK",
                "util_percent": 29.0,
                "temp_c": 380.0,
                "power_w": 71.0,
                "mem_used_mb": 2962.0,
                "mem_total_mb": 21527.0,
                "mem_percent": 13.76,
                "bus_id": "0000:01:00.0",
            },
            {
                "index": 1,
                "chip_id": 0,
                "name": "main",
                "util_percent": 1925564,
                "temp_c": None,
                "power_w": None,
            },
            {
                "index": 1,
                "chip_id": 0,
                "name": "ffmpeg",
                "util_percent": 1925742,
                "temp_c": None,
                "power_w": None,
            },
        ]
        with mock.patch("agent.metrics.npu._run_npu_smi", side_effect=self._fake_npu_smi):
            cards = _sanitize_parsed_metrics([dict(live_cards[0])])
            cards = _enrich_with_typed_queries(cards)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["temp_c"], 71)
        self.assertIsNone(cards[0]["power_w"])
        self.assertEqual(cards[0]["util_percent"], 29.0)
        parsed = _parse_info_table(self.info)
        self.assertEqual(len(parsed), 1)
        self.assertNotIn(1925564, [c.get("util_percent") for c in parsed])
        self.assertNotIn(1925742, [c.get("util_percent") for c in parsed])

    def test_typed_temp_uses_npu_id_not_mcu_chip(self) -> None:
        calls: list = []

        def fake_run(args, timeout=10.0):
            calls.append(list(args))
            return self._fake_npu_smi(args, timeout)

        with mock.patch("agent.metrics.npu._run_npu_smi", side_effect=fake_run):
            collect_npus()
        temp_calls = [c for c in calls if c[:3] == ["info", "-t", "temp"]]
        self.assertTrue(temp_calls, calls)
        for args in temp_calls:
            self.assertIn("-i", args)
            self.assertEqual(args[args.index("-i") + 1], "1")
            if "-c" in args:
                self.assertEqual(args[args.index("-c") + 1], "0")


if __name__ == "__main__":
    unittest.main()
