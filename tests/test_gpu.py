#!/usr/bin/env python3
"""nvidia-smi CSV 解析：实时功耗 + 额定 power.limit。"""

from __future__ import annotations

import os
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.gpu import parse_nvidia_smi_csv


class NvidiaSmiParseTests(unittest.TestCase):
    def test_with_power_limit(self) -> None:
        text = "0, GPU-abc, Tesla T4, 12, 15360, 1024, 45, 30.50, 70.00, 40"
        gpus = parse_nvidia_smi_csv(text, has_power_limit=True)
        self.assertEqual(len(gpus), 1)
        g = gpus[0]
        self.assertEqual(g["index"], 0)
        self.assertEqual(g["util_percent"], 12.0)
        self.assertEqual(g["mem_total_mb"], 15360.0)
        self.assertEqual(g["mem_used_mb"], 1024.0)
        self.assertEqual(g["power_w"], 30.5)
        self.assertEqual(g["power_limit_w"], 70.0)
        self.assertEqual(g["fan_percent"], 40.0)

    def test_without_power_limit(self) -> None:
        text = "0, GPU-abc, Tesla T4, 12, 15360, 1024, 45, 30.50, 40"
        gpus = parse_nvidia_smi_csv(text, has_power_limit=False)
        self.assertEqual(gpus[0]["power_w"], 30.5)
        self.assertIsNone(gpus[0]["power_limit_w"])
        self.assertEqual(gpus[0]["fan_percent"], 40.0)

    def test_na_power_limit(self) -> None:
        text = "0, GPU-x, Device, 0, 8192, 0, 30, [N/A], [N/A], [N/A]"
        gpus = parse_nvidia_smi_csv(text, has_power_limit=True)
        self.assertIsNone(gpus[0]["power_w"])
        self.assertIsNone(gpus[0]["power_limit_w"])
        self.assertIsNone(gpus[0]["fan_percent"])

    def test_auto_detect_ten_columns(self) -> None:
        text = "1, GPU-z, A100, 80, 40960, 20000, 62, 250, 400, N/A"
        gpus = parse_nvidia_smi_csv(text, has_power_limit=False)
        self.assertEqual(gpus[0]["power_w"], 250.0)
        self.assertEqual(gpus[0]["power_limit_w"], 400.0)


if __name__ == "__main__":
    unittest.main()
