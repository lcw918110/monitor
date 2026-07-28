#!/usr/bin/env python3
"""瑞芯微 RKNN / 加速卡解析测试。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.accelerators import parse_rknpu_load


class RknnLoadParseTests(unittest.TestCase):
    def test_multicore(self) -> None:
        text = "NPU load:  Core0:  21%, Core1:  11%, Core2:  0%,"
        p = parse_rknpu_load(text)
        self.assertEqual(p["core_count"], 3)
        self.assertEqual(p["cores"][0]["util_percent"], 21.0)
        self.assertEqual(p["cores"][1]["util_percent"], 11.0)
        self.assertEqual(p["cores"][2]["util_percent"], 0.0)
        self.assertEqual(p["util_percent"], round((21 + 11 + 0) / 3.0, 2))

    def test_single(self) -> None:
        p = parse_rknpu_load("NPU load: 35%")
        self.assertEqual(p["util_percent"], 35.0)


if __name__ == "__main__":
    unittest.main()
