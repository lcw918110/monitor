#!/usr/bin/env python3
"""日/周/月诊断报告：时间窗、5% 空闲阈值、生成后入库再读。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center import reports as reports_mod
from center.reports import (
    HIGH_BUSY_GTE,
    HIGH_P95_GTE,
    IDLE_AVG_LT,
    SHANGHAI,
    high_load_reasons,
    idle_status,
    report_period_bounds,
)
from center.server import make_handler
from center.storage import Storage


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=SHANGHAI).timestamp())


def _host(
    cpu_avg: float,
    cpu_busy: float,
    cpu_n: int = 12,
    cpu_p95: float = 10,
    accel_avg=None,
    accel_busy=None,
    accel_n: int = 0,
    accel_p95=None,
    accel_count: int = 0,
    online: bool = True,
) -> dict:
    return {
        "online": online,
        "accel_count": accel_count,
        "metrics": {
            "cpu_percent": {
                "avg": cpu_avg,
                "p95": cpu_p95,
                "max": cpu_p95,
                "busy_ratio": cpu_busy,
                "sample_count": cpu_n,
            },
            "accel_util_avg": {
                "avg": accel_avg,
                "p95": accel_p95,
                "max": accel_p95,
                "busy_ratio": accel_busy,
                "sample_count": accel_n,
            },
        },
    }


def _post(storage: Storage, host_id: str, ts: int, cpu: float, **extra) -> None:
    system = {
        "cpu_percent": cpu,
        "cpu_count": 8,
        "mem_percent": extra.get("mem_percent", 40),
        "disk_percent": extra.get("disk_percent", 20),
        "load1": 0.4,
        "primary_ip": extra.get("primary_ip"),
    }
    if extra.get("disks") is not None:
        system["disks"] = extra["disks"]
        system["disk_count"] = len(extra["disks"])
    gpus = extra.get("gpus") or []
    api_mod.handle_metrics_post(
        storage,
        json.dumps(
            {
                "host_id": host_id,
                "hostname": extra.get("hostname", host_id),
                "host_type": "gpu" if gpus else "cpu",
                "timestamp": ts,
                "system": system,
                "gpus": gpus,
            }
        ).encode("utf-8"),
    )


class WindowTests(unittest.TestCase):
    def test_day_week_month_half_open_shanghai(self) -> None:
        now = _ts(2026, 10, 3, 15, 30)
        day = report_period_bounds("day", now)
        self.assertEqual(day["from_ts"], _ts(2026, 10, 2))
        self.assertEqual(day["to_ts"], _ts(2026, 10, 3))
        self.assertEqual(day["compare_from_ts"], _ts(2026, 10, 1))
        self.assertEqual(day["compare_to_ts"], _ts(2026, 10, 2))
        self.assertLess(day["from_ts"], day["to_ts"])

        week = report_period_bounds("week", now)
        self.assertEqual(week["from_ts"], _ts(2026, 9, 21))
        self.assertEqual(week["to_ts"], _ts(2026, 9, 28))
        self.assertEqual(week["compare_from_ts"], _ts(2026, 9, 14))
        self.assertEqual(week["compare_to_ts"], _ts(2026, 9, 21))

        month = report_period_bounds("month", now)
        self.assertEqual(month["from_ts"], _ts(2026, 9, 1))
        self.assertEqual(month["to_ts"], _ts(2026, 10, 1))
        self.assertEqual(month["compare_from_ts"], _ts(2026, 8, 1))
        self.assertEqual(month["compare_to_ts"], _ts(2026, 9, 1))

    def test_monday_and_month_start_edges(self) -> None:
        monday = _ts(2026, 9, 28, 10, 0)
        week = report_period_bounds("week", monday)
        self.assertEqual(week["from_ts"], _ts(2026, 9, 21))
        self.assertEqual(week["to_ts"], _ts(2026, 9, 28))
        first = _ts(2026, 10, 1, 8, 0)
        month = report_period_bounds("month", first)
        self.assertEqual(month["from_ts"], _ts(2026, 9, 1))
        self.assertEqual(month["to_ts"], _ts(2026, 10, 1))

    def test_reject_unknown_period(self) -> None:
        with self.assertRaises(ValueError):
            report_period_bounds("year", _ts(2026, 10, 3))


class IdleThresholdTests(unittest.TestCase):
    def test_constants_are_tightened_to_five_percent(self) -> None:
        self.assertEqual(IDLE_AVG_LT, 5.0)
        self.assertEqual(HIGH_BUSY_GTE, 0.40)
        self.assertEqual(HIGH_P95_GTE, 85.0)

    def test_idle_requires_cpu_avg_and_busy_under_five(self) -> None:
        ok, _ = idle_status(_host(4.9, 0.049))
        self.assertTrue(ok)
        ok, _ = idle_status(_host(5.0, 0.0))
        self.assertFalse(ok)
        ok, _ = idle_status(_host(4.9, 0.05))
        self.assertFalse(ok)
        # 旧口径 CPU 平均 <15% 会把 14% 当成基本不用
        ok, _ = idle_status(_host(14.0, 0.01))
        self.assertFalse(ok)

    def test_idle_with_accel_also_under_five(self) -> None:
        ok, _ = idle_status(
            _host(2, 0.0, accel_avg=4.9, accel_busy=0.01, accel_n=12, accel_count=1)
        )
        self.assertTrue(ok)
        # 旧口径加速卡平均 <10% 会把 9% 当成基本不用
        ok, reason = idle_status(
            _host(2, 0.0, accel_avg=9.0, accel_busy=0.01, accel_n=12, accel_count=1)
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "")
        ok, reason = idle_status(
            _host(2, 0.0, accel_avg=None, accel_busy=None, accel_n=0, accel_count=1)
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "加速卡样本不足")

    def test_offline_or_short_samples_are_not_idle(self) -> None:
        ok, reason = idle_status(_host(1, 0.0, online=False))
        self.assertFalse(ok)
        self.assertIn("离线", reason)
        ok, reason = idle_status(_host(1, 0.0, cpu_n=9))
        self.assertFalse(ok)
        self.assertIn("样本不足", reason)

    def test_high_load_busy_or_p95(self) -> None:
        self.assertTrue(high_load_reasons(_host(50, 0.40, cpu_p95=70)))
        self.assertFalse(high_load_reasons(_host(50, 0.399, cpu_p95=84.9)))
        self.assertTrue(high_load_reasons(_host(20, 0.1, cpu_p95=85)))
        reasons = high_load_reasons(
            _host(
                10,
                0.0,
                cpu_p95=20,
                accel_avg=90,
                accel_busy=0.5,
                accel_n=12,
                accel_p95=96,
                accel_count=2,
            )
        )
        self.assertTrue(any("加速卡" in r for r in reasons))
        self.assertFalse(high_load_reasons(_host(90, 0.9, cpu_n=3)))


class GenerateStoreReadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=40,
        )
        self.now = int(time.time())
        bounds = report_period_bounds("day", self.now)
        span = bounds["to_ts"] - bounds["from_ts"]
        self.samples = [
            bounds["from_ts"] + int((span - 2) * i / 12) for i in range(12)
        ]
        idle_gpus = [{"index": 0, "name": "NVIDIA T4", "util_percent": 1, "vendor": "nvidia"}]
        hot_gpus = [{"index": 0, "name": "NVIDIA A100", "util_percent": 95, "vendor": "nvidia"}]
        hot_disks = [
            {"mount": "/data", "percent": 96.0, "used_gb": 960, "total_gb": 1000},
            {"mount": "/", "percent": 20.0, "used_gb": 20, "total_gb": 100},
        ]
        for ts in self.samples:
            _post(
                self.storage,
                "idle-1",
                ts,
                2,
                primary_ip="10.1.2.3",
                disk_percent=10,
                gpus=idle_gpus,
            )
            _post(
                self.storage,
                "hot-1",
                ts,
                90,
                primary_ip="10.1.2.4",
                disk_percent=96,
                disks=hot_disks,
                gpus=hot_gpus,
                mem_percent=90,
            )
            _post(
                self.storage,
                "down-1",
                ts,
                1,
                primary_ip="10.1.2.5",
                disk_percent=30,
            )
        for ts in self.samples[:3]:
            _post(
                self.storage,
                "sparse-1",
                ts,
                1,
                primary_ip="10.1.2.6",
                disk_percent=10,
            )
        _post(self.storage, "idle-1", self.now, 2, primary_ip="10.1.2.3", gpus=idle_gpus)
        _post(
            self.storage,
            "hot-1",
            self.now,
            90,
            primary_ip="10.1.2.4",
            disk_percent=96,
            disks=hot_disks,
            gpus=hot_gpus,
        )
        _post(self.storage, "sparse-1", self.now, 1, primary_ip="10.1.2.6")
        err, _group = self.storage.create_group("训练")
        self.assertIsNone(err)
        self.storage.assign_host_group("hot-1", 1)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _generate(self, period: str = "day", storage: Storage = None):
        body = json.dumps({"period": period, "as_of": self.now}).encode("utf-8")
        return reports_mod.handle_generate_report(
            storage or self.storage,
            body,
            busy_thresholds={"cpu_percent": 80.0, "accel_util_avg": 80.0},
            anomaly_thresholds={
                "disk_warn_percent": 85,
                "disk_critical_percent": 95,
            },
        )

    def test_generate_store_and_reread_without_recompute(self) -> None:
        code, payload = self._generate()
        self.assertEqual(code, 200, payload)
        report = payload["report"]
        md = report["markdown"]
        self.assertIn("使用诊断日报", md)
        self.assertIn("样本加权", md)
        self.assertIn("平均 <5%", md)
        self.assertNotIn("<15%", md)
        self.assertNotIn("<10%", md)
        self.assertIn("10.1.2.3", md)
        self.assertIn("10.1.2.4", md)
        self.assertIn("/data", md)
        self.assertIn("训练", md)
        self.assertIn("磁盘满不等于算力忙", md)
        self.assertIn("不拼接", md)
        self.assertIn("10.1.2.5", md)
        rules = report["payload"]["rules"]
        self.assertEqual(rules["idle_avg_lt"], 5.0)
        self.assertEqual(rules["rollup"], "sample_weighted")
        poles = {row["host_id"]: row["pole"] for row in report["payload"]["rankings"]}
        self.assertEqual(poles["idle-1"], "idle")
        self.assertEqual(poles["hot-1"], "high_load")
        self.assertEqual(poles["down-1"], "offline")
        self.assertEqual(poles["sparse-1"], "insufficient")
        hot = next(r for r in report["payload"]["rankings"] if r["host_id"] == "hot-1")
        self.assertEqual(hot["fullest_mount"], "/data")

        self.storage.cleanup_old_metrics()
        with self.storage._lock:
            conn = self.storage._connect()
            try:
                conn.execute("DELETE FROM metrics_history")
                conn.commit()
            finally:
                conn.close()
        code, again = reports_mod.handle_get_report(self.storage, report["id"])
        self.assertEqual(code, 200)
        self.assertEqual(again["report"]["markdown"], md)

        code, listed = reports_mod.handle_list_reports(self.storage)
        self.assertEqual(listed["reports"][0]["id"], report["id"])
        code, csv_payload = reports_mod.handle_report_csv(self.storage, report["id"])
        self.assertEqual(code, 200)
        self.assertIn("idle-1", csv_payload["csv"])
        self.assertIn("fullest_mount", csv_payload["csv"])

    def test_month_does_not_stitch_saved_day_report(self) -> None:
        self.storage.save_diagnostic_report(
            "day",
            "假日报",
            1,
            2,
            False,
            "FAKE_STITCH_MARKER 这是旧日报，不能拿来拼月报",
            {"rankings": []},
        )
        code, payload = self._generate("month")
        self.assertEqual(code, 200, payload)
        md = payload["report"]["markdown"]
        self.assertNotIn("FAKE_STITCH_MARKER", md)
        self.assertIn("月报", md)
        self.assertTrue(payload["report"]["clamped"] or "裁剪" in md or "保留期" in md)

    def test_retention_clamp_noted(self) -> None:
        short = Storage(
            os.path.join(self.tmp.name, "short.db"),
            offline_seconds=90,
            retention_days=1,
        )
        code, payload = self._generate("day", storage=short)
        self.assertEqual(code, 200, payload)
        self.assertTrue(payload["report"]["clamped"])
        self.assertIn("裁剪", payload["report"]["markdown"])


class HttpReportTests(unittest.TestCase):
    def test_routes_generate_preview_download(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        storage = Storage(
            os.path.join(tmp.name, "t.db"), offline_seconds=90, retention_days=7
        )
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(storage, ""))
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%s" % port
            with urlopen(base + "/api/v1/reports/preview?period=week") as resp:
                preview = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(preview["ok"])
            self.assertEqual(preview["label"], "周报")
            req = Request(
                base + "/api/v1/reports/generate",
                data=json.dumps({"period": "day"}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                created = json.loads(resp.read().decode("utf-8"))
            self.assertTrue(created["ok"])
            rid = created["report"]["id"]
            with urlopen(base + "/api/v1/reports/%s/markdown" % rid) as resp:
                body = resp.read().decode("utf-8")
                disposition = resp.headers.get("Content-Disposition") or ""
            self.assertIn("使用诊断日报", body)
            self.assertIn(".md", disposition)
            with urlopen(base + "/") as resp:
                page = resp.read().decode("utf-8")
            self.assertIn("使用诊断", page)
            self.assertIn("tabBtnReports", page)
        finally:
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
