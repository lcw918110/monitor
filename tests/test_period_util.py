#!/usr/bin/env python3
"""时段利用：绝对时间窗、P95、busy_ratio、集群样本加权汇总。"""

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
from center.storage import Storage, aggregate_series, percentile


def _post(storage: Storage, host_id: str, ts: int, cpu: float, **extra) -> None:
    system = {
        "cpu_percent": cpu,
        "cpu_count": extra.get("cpu_count", 8),
        "mem_percent": extra.get("mem_percent", 40),
        "disk_percent": extra.get("disk_percent", 20),
        "load1": extra.get("load1", 1.0),
        "net_rx_mbps": extra.get("net_rx_mbps"),
        "net_tx_mbps": extra.get("net_tx_mbps"),
        "net_rated_mbps": extra.get("net_rated_mbps"),
        "net_rx_percent": extra.get("net_rx_percent"),
        "net_tx_percent": extra.get("net_tx_percent"),
    }
    npus = extra.get("npus", [])
    gpus = extra.get("gpus", [])
    api_mod.handle_metrics_post(
        storage,
        json.dumps(
            {
                "host_id": host_id,
                "hostname": extra.get("hostname", host_id),
                "host_type": extra.get("host_type", "app"),
                "timestamp": ts,
                "system": system,
                "npus": npus,
                "gpus": gpus,
            }
        ).encode("utf-8"),
    )


class PercentileTests(unittest.TestCase):
    def test_p95_linear_interpolation(self) -> None:
        nums = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        # rank = 0.95 * 9 = 8.55 → 90 + 0.55 * 10 = 95.5
        self.assertEqual(percentile(nums, 95), 95.5)

    def test_aggregate_busy_ratio(self) -> None:
        nums = [10, 50, 80, 90, 100]
        agg = aggregate_series(nums, busy_threshold=80)
        self.assertEqual(agg["avg"], 66)
        self.assertEqual(agg["min"], 10)
        self.assertEqual(agg["max"], 100)
        self.assertEqual(agg["busy_ratio"], 0.6)
        self.assertEqual(agg["sample_count"], 5)


class PeriodWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=7,
        )
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_relative_minutes_still_works(self) -> None:
        for i in range(5):
            _post(self.storage, "h1", self.now - (5 - i) * 30, 40)
        err, window = self.storage.resolve_period_window(minutes=60, now=self.now)
        self.assertIsNone(err)
        self.assertEqual(window["mode"], "relative")
        self.assertEqual(window["minutes"], 60)
        self.assertEqual(window["from_ts"], self.now - 3600)
        self.assertEqual(window["to_ts"], self.now)

        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "minutes=60"
        )
        self.assertEqual(code, 200)
        self.assertTrue(pstats["ok"])
        self.assertGreaterEqual(pstats["sample_count"], 5)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["avg"], 40)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["max"], 40)
        self.assertIn("p95", pstats["metrics"]["cpu_percent"])
        self.assertIn("busy_ratio", pstats["metrics"]["cpu_percent"])

    def test_absolute_range(self) -> None:
        for ts, cpu in (
            (self.now - 400, 10),
            (self.now - 300, 20),
            (self.now - 200, 90),
            (self.now - 100, 40),
        ):
            _post(self.storage, "h1", ts, cpu)
        from_ts = self.now - 350
        to_ts = self.now - 150
        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "from_ts=%s&to_ts=%s" % (from_ts, to_ts)
        )
        self.assertEqual(code, 200)
        self.assertEqual(pstats["sample_count"], 2)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["min"], 20)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["max"], 90)
        self.assertEqual(pstats["window"]["mode"], "absolute")
        self.assertEqual(pstats["window"]["from_ts"], from_ts)
        self.assertEqual(pstats["window"]["to_ts"], to_ts)

    def test_absolute_from_without_to(self) -> None:
        _post(self.storage, "h1", self.now - 10, 10)
        code, payload = api_mod.handle_host_period_stats(
            self.storage, "h1", "from_ts=%s" % (self.now - 100,)
        )
        self.assertEqual(code, 400)
        self.assertIn("from_ts", payload["error"])

    def test_invalid_range(self) -> None:
        _post(self.storage, "h1", self.now - 10, 10)
        code, payload = api_mod.handle_host_period_stats(
            self.storage, "h1", "from_ts=%s&to_ts=%s" % (self.now, self.now - 10)
        )
        self.assertEqual(code, 400)

    def test_clamp_to_retention(self) -> None:
        err, window = self.storage.resolve_period_window(
            from_ts=self.now - 20 * 86400,
            to_ts=self.now - 86400,
            now=self.now,
        )
        self.assertIsNone(err)
        self.assertTrue(window["clamped"])
        self.assertEqual(window["from_ts"], self.now - 7 * 86400)
        self.assertEqual(window["to_ts"], self.now - 86400)

    def test_range_outside_retention(self) -> None:
        err, window = self.storage.resolve_period_window(
            from_ts=self.now - 20 * 86400,
            to_ts=self.now - 15 * 86400,
            now=self.now,
        )
        self.assertIsNotNone(err)
        self.assertIn("保留期", err)
        self.assertEqual(window, {})


class PeriodAggAndClusterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=7,
        )
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_p95_and_busy_ratio_on_host(self) -> None:
        values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
        for i, cpu in enumerate(values):
            _post(self.storage, "h1", self.now - (len(values) - i) * 10, cpu)
        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "minutes=60&busy_cpu=80"
        )
        self.assertEqual(code, 200)
        cpu = pstats["metrics"]["cpu_percent"]
        self.assertEqual(cpu["p95"], 95.5)
        self.assertEqual(cpu["busy_ratio"], 0.3)  # 80,90,100 → 3/10
        self.assertEqual(cpu["avg"], 55)

    def test_busy_threshold_override(self) -> None:
        for i, cpu in enumerate([10, 50, 90]):
            _post(self.storage, "h1", self.now - (3 - i) * 10, cpu)
        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "minutes=60&busy_cpu=90"
        )
        self.assertEqual(pstats["metrics"]["cpu_percent"]["busy_ratio"], round(1 / 3, 4))

    def test_cluster_sample_weighted_rollup(self) -> None:
        # h1: 3 samples of 20 → avg 20; h2: 1 sample of 80 → avg 80
        # sample-weighted avg = (20*3 + 80)/4 = 35; avg-of-avgs would be 50
        for i in range(3):
            _post(self.storage, "h1", self.now - 30 + i, 20, hostname="one")
        _post(self.storage, "h2", self.now - 5, 80, hostname="two")
        code, payload = api_mod.handle_cluster_period_stats(
            self.storage, "minutes=60"
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["rollup"], "sample_weighted")
        self.assertEqual(payload["host_count"], 2)
        self.assertEqual(payload["sample_count"], 4)
        self.assertEqual(payload["cluster"]["metrics"]["cpu_percent"]["avg"], 35)
        self.assertEqual(payload["cluster"]["metrics"]["cpu_percent"]["min"], 20)
        self.assertEqual(payload["cluster"]["metrics"]["cpu_percent"]["max"], 80)
        hosts = {h["host_id"]: h for h in payload["hosts"]}
        self.assertEqual(hosts["h1"]["metrics"]["cpu_percent"]["avg"], 20)
        self.assertEqual(hosts["h2"]["metrics"]["cpu_percent"]["avg"], 80)

    def test_cluster_host_ids_filter(self) -> None:
        _post(self.storage, "h1", self.now - 10, 10)
        _post(self.storage, "h2", self.now - 10, 90)
        code, payload = api_mod.handle_cluster_period_stats(
            self.storage, "minutes=60&host_ids=h2"
        )
        self.assertEqual(code, 200)
        self.assertEqual(payload["host_count"], 1)
        self.assertEqual(payload["hosts"][0]["host_id"], "h2")
        self.assertEqual(payload["cluster"]["metrics"]["cpu_percent"]["avg"], 90)

    def test_rated_percent_from_compact(self) -> None:
        _post(
            self.storage,
            "h1",
            self.now - 20,
            10,
            net_rx_mbps=100,
            net_tx_mbps=50,
            net_rated_mbps=1000,
        )
        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "minutes=60"
        )
        self.assertEqual(code, 200)
        self.assertEqual(pstats["metrics"]["net_rx_percent"]["avg"], 10)
        self.assertEqual(pstats["metrics"]["net_tx_percent"]["avg"], 5)
        self.assertEqual(pstats["metrics"]["net_rated_mbps"]["avg"], 1000)

    def test_rated_fallback_from_latest_snapshot(self) -> None:
        # 历史点只有吞吐、无额定；最新快照带额定 → 按时段回填利用率
        _post(
            self.storage,
            "h1",
            self.now - 40,
            10,
            net_rx_mbps=200,
            net_tx_mbps=100,
        )
        _post(
            self.storage,
            "h1",
            self.now - 10,
            10,
            net_rx_mbps=200,
            net_tx_mbps=100,
            net_rated_mbps=1000,
        )
        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "h1", "minutes=60"
        )
        self.assertEqual(code, 200)
        # 第一点缺额定，用最新快照 1000 回填 → 20%；第二点也是 20%
        self.assertEqual(pstats["metrics"]["net_rx_percent"]["avg"], 20)

    def test_accel_busy_ratio(self) -> None:
        for i, util in enumerate([10, 85, 90]):
            _post(
                self.storage,
                "gpu-1",
                self.now - (3 - i) * 10,
                30,
                host_type="gpu",
                gpus=[{"index": 0, "util_percent": util, "temp_c": 50 + i}],
            )
        code, pstats = api_mod.handle_host_period_stats(
            self.storage, "gpu-1", "minutes=60&busy_accel=80"
        )
        self.assertEqual(code, 200)
        accel = pstats["metrics"]["accel_util_avg"]
        self.assertEqual(accel["busy_ratio"], round(2 / 3, 4))
        self.assertEqual(accel["max"], 90)
        self.assertEqual(pstats["metrics"]["accel_temp_max"]["max"], 52)

    def test_export_csv(self) -> None:
        _post(self.storage, "h1", self.now - 10, 40, hostname="one")
        _post(self.storage, "h2", self.now - 10, 80, hostname="two")
        code, text = api_mod.handle_export_period_csv(self.storage, "minutes=60")
        self.assertEqual(code, 200)
        self.assertIn("host_id", text)
        self.assertIn("__cluster__", text)
        self.assertIn("cpu_percent_p95", text)
        self.assertIn("cpu_percent_busy_ratio", text)
        self.assertIn("h1", text)
        self.assertIn("h2", text)
        self.assertIn("one", text)

    def test_absolute_and_minutes_absolute_wins(self) -> None:
        _post(self.storage, "h1", self.now - 100, 11)
        _post(self.storage, "h1", self.now - 10, 99)
        code, pstats = api_mod.handle_host_period_stats(
            self.storage,
            "h1",
            "minutes=1&from_ts=%s&to_ts=%s" % (self.now - 150, self.now - 50),
        )
        self.assertEqual(code, 200)
        self.assertEqual(pstats["sample_count"], 1)
        self.assertEqual(pstats["metrics"]["cpu_percent"]["avg"], 11)


if __name__ == "__main__":
    unittest.main()
