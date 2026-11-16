#!/usr/bin/env python3
"""磁盘读写：/proc/diskstats 解析、整盘汇总、历史上报缺字段。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.disk_io import (
    collect_disk_io,
    is_whole_data_disk,
    parse_proc_diskstats,
    rates_between,
)
from center import api as api_mod
from center.storage import Storage


# 14 列是老内核；nvme 行多了 discard。分区、loop、zram、sr 不应进入整盘。
DISKSTATS_A = textwrap.dedent(
    """\
       8       0 sda 1000 0 2000 10 100 0 1000 5 0 1000 1000 0
       8       1 sda1 900 0 1800 8 80 0 800 4 0 800 800 0
       8      16 sdb 10 0 80 1 0 0 x 0 0 10 10
     259       0 nvme0n1 50 0 400 2 20 0 160 1 0 40 40 0 0 0 0
     259       1 nvme0n1p1 50 0 400 2 20 0 160 1 0 40 40 0 0 0 0
       7       0 loop0 9 0 72 1 9 0 72 1 0 9 9 0
     252       0 zram0 9 0 72 1 9 0 72 1 0 9 9
      11       0 sr0 1 0 8 1 0 0 0 0 0 1 1 0
     253       0 vda 5 0 40 1 5 0 40 1 0 5 5 0
     259       2 nvme1n1 1 0 8 1 1 0 8 1 0 1 1
     179       0 mmcblk0 3 0 24 1 3 0 24 1 0 3 3 0
     179       1 mmcblk0p1 3 0 24 1 3 0 24 1 0 3 3 0
       8      32 xvda 4 0 32 1 4 0 32 1 0 4 4 0
       8      33 xvda1 4 0 32 1 4 0 32 1 0 4 4 0
    broken line
       8      48 sdc 1 0 not-a-number 0 1 0 16 1 0 2 2
    """
)

# 1 秒后：sda 读扇区 +2000 → 8.192 Mbps；写 +1000 → 4.096 Mbps；io_ms +250 → 25%。
# sdb 的写扇区不是数字，两笔都算不出写速率。io_ms +1000 → 100%，主机忙闲取最大。
# sda1 的巨大增量必须被分区过滤掉。
DISKSTATS_B = textwrap.dedent(
    """\
       8       0 sda 1100 0 4000 20 200 0 2000 10 0 1250 1400 0
       8       1 sda1 5000 0 800000 80 80 0 800 4 0 9000 9000 0
       8      16 sdb 10 0 80 1 10 0 x 0 0 1010 1010
     259       0 nvme0n1 50 0 400 2 20 0 160 1 0 40 40 0 0 0 0
     253       0 vda 5 0 40 1 5 0 40 1 0 5 5 0
     259       2 nvme1n1 1 0 8 1 1 0 8 1 0 1 1
     179       0 mmcblk0 3 0 24 1 3 0 24 1 0 3 3 0
       8      32 xvda 4 0 32 1 4 0 32 1 0 4 4 0
       8      48 sdc 1 0 not-a-number 0 1 0 16 1 0 2 2
    """
)


class DiskstatsParseTests(unittest.TestCase):
    def test_whole_disk_names(self) -> None:
        self.assertTrue(is_whole_data_disk("sda"))
        self.assertTrue(is_whole_data_disk("nvme0n1"))
        self.assertTrue(is_whole_data_disk("nvme1n1"))
        self.assertTrue(is_whole_data_disk("vda"))
        self.assertTrue(is_whole_data_disk("xvda"))
        self.assertTrue(is_whole_data_disk("mmcblk0"))
        self.assertFalse(is_whole_data_disk("sda1"))
        self.assertFalse(is_whole_data_disk("nvme0n1p1"))
        self.assertFalse(is_whole_data_disk("mmcblk0p1"))
        self.assertFalse(is_whole_data_disk("xvda1"))
        self.assertFalse(is_whole_data_disk("loop0"))
        self.assertFalse(is_whole_data_disk("ram0"))
        self.assertFalse(is_whole_data_disk("zram0"))
        self.assertFalse(is_whole_data_disk("sr0"))
        self.assertFalse(is_whole_data_disk("dm-0"))
        self.assertFalse(is_whole_data_disk("md0"))

    def test_parse_keeps_partial_rows(self) -> None:
        parsed = parse_proc_diskstats(DISKSTATS_A)
        self.assertIn("sda", parsed)
        self.assertIn("nvme0n1", parsed)
        self.assertEqual(parsed["sda"]["read_sectors"], 2000)
        self.assertEqual(parsed["sda"]["write_sectors"], 1000)
        self.assertEqual(parsed["sda"]["io_ms"], 1000)
        self.assertIn("sdb", parsed)
        self.assertIsNone(parsed["sdb"]["write_sectors"])
        self.assertEqual(parsed["sdb"]["read_sectors"], 80)
        self.assertIn("sdc", parsed)
        self.assertIsNone(parsed["sdc"]["read_sectors"])
        self.assertEqual(parsed["sdc"]["write_sectors"], 16)
        self.assertNotIn("broken", parsed)

    def test_rates_sum_reads_and_max_util(self) -> None:
        prev = parse_proc_diskstats(DISKSTATS_A)
        curr = parse_proc_diskstats(DISKSTATS_B)
        rates = rates_between(prev, curr, 1.0)
        names = [item["name"] for item in rates["disk_io"]]
        self.assertIn("sda", names)
        self.assertNotIn("sda1", names)
        self.assertNotIn("loop0", names)
        self.assertNotIn("zram0", names)
        self.assertNotIn("sr0", names)
        sda = next(item for item in rates["disk_io"] if item["name"] == "sda")
        self.assertEqual(sda["read_mbps"], 8.192)
        self.assertEqual(sda["write_mbps"], 4.096)
        self.assertEqual(sda["util_percent"], 25.0)
        sdb = next(item for item in rates["disk_io"] if item["name"] == "sdb")
        self.assertEqual(sdb["read_mbps"], 0.0)
        self.assertIsNone(sdb["write_mbps"])
        self.assertEqual(sdb["util_percent"], 100.0)
        self.assertEqual(rates["disk_io_util_percent"], 100.0)
        # sda 8.192 + 其余整盘增量为 0。分区的 80 万扇区不能加进来。
        self.assertEqual(rates["disk_read_mbps"], 8.192)
        self.assertEqual(rates["disk_write_mbps"], 4.096)

    def test_first_sample_and_counter_reset_are_not_invented(self) -> None:
        prev = parse_proc_diskstats(DISKSTATS_A)
        curr = parse_proc_diskstats(DISKSTATS_B)
        self.assertIsNone(rates_between({}, curr, 1.0)["disk_read_mbps"])
        self.assertEqual(rates_between({}, curr, 1.0)["disk_io"], [])
        self.assertIsNone(rates_between(prev, curr, 0)["disk_write_mbps"])
        wrapped = dict(curr)
        wrapped["sda"] = dict(curr["sda"])
        wrapped["sda"]["read_sectors"] = 1
        rates = rates_between({"sda": prev["sda"]}, {"sda": wrapped["sda"]}, 1.0)
        self.assertIsNone(rates["disk_read_mbps"])
        self.assertEqual(rates["disk_write_mbps"], 4.096)

    def test_util_caps_at_100(self) -> None:
        prev = {"sda": {"read_sectors": 0, "write_sectors": 0, "io_ms": 0}}
        curr = {"sda": {"read_sectors": 0, "write_sectors": 0, "io_ms": 5000}}
        rates = rates_between(prev, curr, 1.0)
        self.assertEqual(rates["disk_io_util_percent"], 100.0)
        self.assertEqual(rates["disk_read_mbps"], 0.0)

    def test_collect_uses_previous_snapshot(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        path = os.path.join(tmp.name, "diskstats")
        state = {}
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(DISKSTATS_A)
        first = collect_disk_io(path=path, now=10, state=state)
        self.assertIsNone(first["disk_read_mbps"])
        self.assertEqual(first["disk_io"], [])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(DISKSTATS_B)
        second = collect_disk_io(path=path, now=11, state=state)
        self.assertEqual(second["disk_read_mbps"], 8.192)
        self.assertEqual(second["disk_io_util_percent"], 100.0)
        missing = collect_disk_io(path=path + ".missing", now=12, state=state)
        self.assertIsNone(missing["disk_read_mbps"])
        self.assertIsNotNone(state.get("mono"))
        tmp.cleanup()


class DiskIoHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "t.db"), retention_days=7)
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _post(self, ts: int, system: dict) -> None:
        api_mod.handle_metrics_post(
            self.storage,
            json.dumps(
                {
                    "host_id": "io-1",
                    "hostname": "io-1",
                    "host_type": "cpu",
                    "timestamp": ts,
                    "system": system,
                }
            ).encode("utf-8"),
        )

    def test_old_point_without_io_stays_empty(self) -> None:
        self._post(
            self.now - 30,
            {"cpu_percent": 12, "mem_percent": 20, "disk_percent": 40},
        )
        points = self.storage.host_history("io-1", minutes=60)
        self.assertEqual(len(points), 1)
        self.assertIsNone(points[0]["disk_read_mbps"])
        self.assertIsNone(points[0]["disk_write_mbps"])
        self.assertIsNone(points[0]["disk_io_util_percent"])
        code, stats = api_mod.handle_host_period_stats(self.storage, "io-1", "minutes=60")
        self.assertEqual(code, 200)
        self.assertEqual(stats["metrics"]["cpu_percent"]["avg"], 12)
        self.assertIsNone(stats["metrics"]["disk_read_mbps"]["avg"])
        self.assertEqual(stats["metrics"]["disk_read_mbps"]["sample_count"], 0)
        self.assertIsNone(stats["metrics"]["disk_io_util_percent"]["busy_ratio"])

    def test_period_stats_avg_and_ignore_out_of_range_util(self) -> None:
        self._post(
            self.now - 40,
            {
                "cpu_percent": 10,
                "disk_percent": 22,
                "disk_read_mbps": 8.192,
                "disk_write_mbps": 1,
                "disk_io_util_percent": 25,
                "disk_io": [
                    {"name": "sda", "read_mbps": 8.192, "write_mbps": 1, "util_percent": 25}
                ],
            },
        )
        self._post(
            self.now - 20,
            {
                "cpu_percent": 30,
                "disk_percent": 22,
                "disk_read_mbps": None,
                "disk_write_mbps": 3,
                "disk_io_util_percent": 150,
            },
        )
        code, stats = api_mod.handle_host_period_stats(self.storage, "io-1", "minutes=60")
        self.assertEqual(code, 200)
        self.assertEqual(stats["metrics"]["cpu_percent"]["avg"], 20)
        self.assertEqual(stats["metrics"]["disk_percent"]["avg"], 22)
        self.assertEqual(stats["metrics"]["disk_read_mbps"]["avg"], 8.19)
        self.assertEqual(stats["metrics"]["disk_read_mbps"]["sample_count"], 1)
        self.assertEqual(stats["metrics"]["disk_write_mbps"]["avg"], 2)
        self.assertEqual(stats["metrics"]["disk_io_util_percent"]["avg"], 25)
        self.assertEqual(stats["metrics"]["disk_io_util_percent"]["sample_count"], 1)
        self.assertIsNone(stats["metrics"]["disk_io_util_percent"]["busy_ratio"])
        points = self.storage.host_history("io-1", minutes=60)
        first = points[0]
        self.assertEqual(first["disk_io"][0]["name"], "sda")
        self.assertIsNone(points[1]["disk_read_mbps"])


if __name__ == "__main__":
    unittest.main()
