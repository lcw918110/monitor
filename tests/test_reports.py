#!/usr/bin/env python3
"""日/周/月诊断报告：时间窗、5% 空闲阈值、生成后入库再读。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.request import Request, urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center import reports as reports_mod
from center.report_schedule import (
    ReportScheduler,
    due_slots,
    normalize_report_schedule,
)
from center.reports import (
    HIGH_BUSY_GTE,
    HIGH_P95_GTE,
    IDLE_AVG_LT,
    SHANGHAI,
    high_load_reasons,
    idle_status,
    report_period_bounds,
    report_window_for_date,
)
from center.server import load_config, make_handler
from center.storage import Storage
from common.validate import validate_center_config


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
    npus = extra.get("npus") or []
    api_mod.handle_metrics_post(
        storage,
        json.dumps(
            {
                "host_id": host_id,
                "hostname": extra.get("hostname", host_id),
                "host_type": "gpu" if (gpus or npus) else "cpu",
                "timestamp": ts,
                "system": system,
                "gpus": gpus,
                "npus": npus,
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
        linked = []
        for section in report["payload"]["sections"]:
            for table in section.get("tables") or []:
                ids = table.get("host_ids")
                if not ids:
                    continue
                self.assertEqual(len(ids), len(table.get("rows") or []))
                linked.extend(item for item in ids if item)
        self.assertIn("hot-1", linked)
        self.assertIn("idle-1", linked)
        self.assertNotIn("host_ids", report["markdown"])

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
            self.assertIn("reportWindowDate", page)
            self.assertIn("reportScheduleNote", page)
        finally:
            httpd.shutdown()
            httpd.server_close()
            tmp.cleanup()


class SchedulerWindowTests(unittest.TestCase):
    def test_defaults_fire_only_in_that_minute(self) -> None:
        spec = normalize_report_schedule(None)
        self.assertEqual(
            [s["period"] for s in due_slots(spec, _ts(2026, 10, 5, 0, 9))], []
        )
        self.assertEqual(
            [s["period"] for s in due_slots(spec, _ts(2026, 10, 5, 0, 11))], []
        )
        daily = due_slots(spec, _ts(2026, 10, 5, 0, 10))
        self.assertEqual([s["period"] for s in daily], ["day"])
        self.assertEqual(daily[0]["from_ts"], _ts(2026, 10, 4))
        self.assertEqual(daily[0]["to_ts"], _ts(2026, 10, 5))

        weekly = due_slots(spec, _ts(2026, 10, 5, 0, 20))
        self.assertEqual([s["period"] for s in weekly], ["week"])
        self.assertEqual(weekly[0]["from_ts"], _ts(2026, 9, 28))
        self.assertEqual(weekly[0]["to_ts"], _ts(2026, 10, 5))
        # 周日的 00:20 不是周报
        self.assertEqual(due_slots(spec, _ts(2026, 10, 4, 0, 20)), [])

        monthly = due_slots(spec, _ts(2026, 10, 1, 0, 30))
        self.assertEqual([s["period"] for s in monthly], ["month"])
        self.assertEqual(monthly[0]["from_ts"], _ts(2026, 9, 1))
        self.assertEqual(monthly[0]["to_ts"], _ts(2026, 10, 1))
        self.assertEqual(due_slots(spec, _ts(2026, 10, 2, 0, 30)), [])

    def test_disabled_and_shared_minute(self) -> None:
        self.assertEqual(
            due_slots({"enabled": False}, _ts(2026, 10, 5, 0, 10)), []
        )
        daily_off = normalize_report_schedule({"daily": {"enabled": False}})
        self.assertEqual(due_slots(daily_off, _ts(2026, 10, 5, 0, 10)), [])
        both = normalize_report_schedule({"weekly": {"minute": 10}})
        self.assertEqual(
            [s["period"] for s in due_slots(both, _ts(2026, 10, 5, 0, 10))],
            ["day", "week"],
        )

    def test_missed_minutes_are_not_backfilled(self) -> None:
        spec = normalize_report_schedule(None)
        fired = set()
        seen = []
        for moment in (
            _ts(2026, 10, 3, 0, 10),
            _ts(2026, 10, 5, 0, 10),
            _ts(2026, 10, 5, 0, 20),
            _ts(2026, 10, 5, 12, 0),
        ):
            for slot in due_slots(spec, moment):
                if slot["key"] in fired:
                    continue
                fired.add(slot["key"])
                seen.append((slot["period"], slot["from_ts"]))
        self.assertEqual(
            seen,
            [
                ("day", _ts(2026, 10, 2)),
                ("day", _ts(2026, 10, 4)),
                ("week", _ts(2026, 9, 28)),
            ],
        )

    def test_explicit_window_date(self) -> None:
        day = report_window_for_date("day", "2026-09-15", _ts(2026, 10, 12, 9, 0))
        self.assertEqual(day["from_ts"], _ts(2026, 9, 15))
        self.assertEqual(day["to_ts"], _ts(2026, 9, 16))
        week = report_window_for_date("week", "2026-09-16", _ts(2026, 10, 12))
        self.assertEqual(week["from_ts"], _ts(2026, 9, 14))
        self.assertEqual(week["to_ts"], _ts(2026, 9, 21))
        month = report_window_for_date("month", "2026-09-15", _ts(2026, 10, 12))
        self.assertEqual(month["from_ts"], _ts(2026, 9, 1))
        self.assertEqual(month["to_ts"], _ts(2026, 10, 1))
        with self.assertRaises(ValueError):
            report_window_for_date("day", "2026-10-12", _ts(2026, 10, 12, 15, 0))
        with self.assertRaises(ValueError):
            report_window_for_date("day", "09-15", _ts(2026, 10, 12))


class SchedulerRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=40,
        )
        self.clock = {"ts": _ts(2026, 10, 5, 0, 10)}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _scheduler(self) -> ReportScheduler:
        return ReportScheduler(
            self.storage,
            clock=lambda: self.clock["ts"],
            busy_thresholds={"cpu_percent": 80.0, "accel_util_avg": 80.0},
        )

    def test_run_due_skips_misses_and_existing_rows(self) -> None:
        sched = self._scheduler()
        self.clock["ts"] = _ts(2026, 10, 5, 12, 0)
        self.assertEqual(sched.run_due(self.clock["ts"]), [])
        self.assertEqual(self.storage.list_diagnostic_reports(), [])

        fire = _ts(2026, 10, 5, 0, 10)
        self.clock["ts"] = fire
        with mock.patch("center.report_schedule.deliver_report") as hook:
            created = sched.run_due(fire)
        self.assertEqual(len(created), 1)
        report = created[0]["report"]
        self.assertEqual(report["source"], "scheduled")
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["generated_at"], fire)
        self.assertEqual(report["requested_from_ts"], _ts(2026, 10, 4))
        self.assertNotEqual(report["generated_at"], report["requested_to_ts"])
        self.assertIn("实际生成时刻（Asia/Shanghai）2026-10-05 00:10", report["markdown"])
        self.assertEqual(hook.call_count, 1)
        self.assertEqual(sched.run_due(fire), [])

        again = self._scheduler()
        with mock.patch("center.report_schedule.deliver_report") as hook:
            skipped = again.run_due(fire)
        self.assertTrue(skipped[0]["skipped"])
        hook.assert_not_called()
        self.assertEqual(len(self.storage.list_diagnostic_reports()), 1)
        self.assertEqual(
            self.storage.get_diagnostic_report(report["id"])["markdown"],
            report["markdown"],
        )

        manual_at = fire + 7200
        self.clock["ts"] = manual_at
        code, payload = reports_mod.handle_generate_report(
            self.storage,
            json.dumps({"period": "day", "as_of": fire}).encode("utf-8"),
            source="manual",
            generated_at=manual_at,
            clock=manual_at,
        )
        self.assertEqual(code, 200, payload)
        self.assertEqual(payload["report"]["id"], report["id"])
        self.assertEqual(payload["report"]["action"], "replaced")
        self.assertEqual(payload["report"]["source"], "manual")
        self.assertEqual(payload["report"]["generated_at"], manual_at)

        third = self._scheduler()
        self.clock["ts"] = fire
        skipped = third.run_due(fire)
        self.assertTrue(skipped[0]["skipped"])
        kept = self.storage.get_diagnostic_report(report["id"])
        self.assertEqual(kept["source"], "manual")
        self.assertEqual(kept["generated_at"], manual_at)

    def test_failed_schedule_is_recorded_and_not_delivered(self) -> None:
        short = Storage(
            os.path.join(self.tmp.name, "short.db"),
            offline_seconds=90,
            retention_days=1,
        )
        self.clock["ts"] = _ts(2026, 11, 20, 12, 0)
        sched = ReportScheduler(short, clock=lambda: self.clock["ts"])
        fire = _ts(2026, 10, 1, 0, 30)
        with mock.patch("center.report_schedule.deliver_report") as hook:
            result = sched.run_due(fire)
        hook.assert_not_called()
        self.assertFalse(result[0]["ok"])
        report = result[0]["report"]
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["source"], "scheduled")
        self.assertTrue(report["error"])
        self.assertIn(report["error"], report["markdown"])
        listed = short.list_diagnostic_reports()
        self.assertEqual(listed[0]["status"], "failed")
        self.assertEqual(listed[0]["error"], report["error"])


class GeneratedAtAndDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=40,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_generated_at_is_not_as_of(self) -> None:
        as_of = _ts(2026, 10, 1, 0, 0)
        generated = _ts(2026, 10, 12, 15, 4)
        code, payload = reports_mod.handle_generate_report(
            self.storage,
            json.dumps({"period": "day", "as_of": as_of}).encode("utf-8"),
            generated_at=generated,
            clock=generated,
        )
        self.assertEqual(code, 200, payload)
        report = payload["report"]
        self.assertEqual(report["generated_at"], generated)
        self.assertEqual(report["payload"]["generated_at"], generated)
        self.assertEqual(report["requested_from_ts"], _ts(2026, 9, 30))
        self.assertNotEqual(report["generated_at"], as_of)
        self.assertIn("实际生成时刻（Asia/Shanghai）2026-10-12 15:04", report["markdown"])
        self.assertNotIn("实际生成时刻（Asia/Shanghai）2026-10-01 00:00", report["markdown"])
        self.assertIn("来源：手动", report["markdown"])

    def test_same_window_replaces_one_row(self) -> None:
        body = json.dumps(
            {"period": "day", "window_date": "2026-09-30"}
        ).encode("utf-8")
        code, first = reports_mod.handle_generate_report(
            self.storage, body, generated_at=_ts(2026, 10, 12, 8, 0), clock=_ts(2026, 10, 12, 8, 0)
        )
        self.assertEqual(code, 200, first)
        code, second = reports_mod.handle_generate_report(
            self.storage, body, generated_at=_ts(2026, 10, 12, 9, 0), clock=_ts(2026, 10, 12, 9, 0)
        )
        self.assertEqual(code, 200, second)
        self.assertEqual(second["report"]["action"], "replaced")
        self.assertEqual(second["report"]["id"], first["report"]["id"])
        self.assertEqual(second["report"]["generated_at"], _ts(2026, 10, 12, 9, 0))
        self.assertEqual(len(self.storage.list_diagnostic_reports()), 1)
        with self.storage._lock:
            conn = self.storage._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM diagnostic_reports").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(count, 1)

    def test_replace_drops_older_duplicate_rows(self) -> None:
        with self.storage._lock:
            conn = self.storage._connect()
            try:
                for created in (10, 20):
                    conn.execute(
                        """
                        INSERT INTO diagnostic_reports (
                            period_type, title, requested_from_ts, requested_to_ts,
                            clamped, created_at, markdown, payload,
                            source, status, error, generated_at
                        ) VALUES ('day', '旧', ?, ?, 0, ?, 'OLD', '{}', 'manual', 'ok', NULL, ?)
                        """,
                        (_ts(2026, 9, 30), _ts(2026, 10, 1), created, created),
                    )
                conn.commit()
            finally:
                conn.close()
        self.assertEqual(len(self.storage.list_diagnostic_reports()), 1)
        code, payload = reports_mod.handle_generate_report(
            self.storage,
            json.dumps({"period": "day", "window_date": "2026-09-30"}).encode("utf-8"),
            generated_at=_ts(2026, 10, 12, 10, 0),
            clock=_ts(2026, 10, 12, 10, 0),
        )
        self.assertEqual(code, 200, payload)
        self.assertNotIn("OLD", payload["report"]["markdown"])
        with self.storage._lock:
            conn = self.storage._connect()
            try:
                count = conn.execute("SELECT COUNT(*) FROM diagnostic_reports").fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(count, 1)

    def test_open_window_is_rejected_without_a_row(self) -> None:
        code, payload = reports_mod.handle_generate_report(
            self.storage,
            json.dumps({"period": "day", "window_date": "2026-10-12"}).encode("utf-8"),
            clock=_ts(2026, 10, 12, 15, 0),
        )
        self.assertEqual(code, 400)
        self.assertIn("尚未结束", payload["error"])
        self.assertNotIn("report", payload)
        self.assertEqual(self.storage.list_diagnostic_reports(), [])


class OutlierExclusionTests(unittest.TestCase):
    def test_out_of_range_util_is_excluded_and_named(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        storage = Storage(
            os.path.join(tmp.name, "t.db"), offline_seconds=90, retention_days=40
        )
        now = _ts(2026, 10, 3, 12, 0)
        bounds = report_period_bounds("day", now)
        span = bounds["to_ts"] - bounds["from_ts"]
        samples = [bounds["from_ts"] + int((span - 2) * i / 12) for i in range(12)]
        try:
            for i, ts in enumerate(samples):
                util = 1283784.7 if i == 0 else 10.0
                cpu = -3.0 if i == 0 else 20.0
                _post(
                    storage,
                    "center-95",
                    ts,
                    cpu,
                    primary_ip="10.9.9.9",
                    hostname="center-95",
                    npus=[
                        {
                            "index": 1,
                            "name": "310P3",
                            "util_percent": util,
                            "vendor": "huawei",
                        }
                    ],
                )
            code, payload = reports_mod.handle_generate_report(
                storage,
                json.dumps({"period": "day", "as_of": now}).encode("utf-8"),
                busy_thresholds={"cpu_percent": 80.0, "accel_util_avg": 80.0},
                generated_at=now,
                clock=now,
            )
            self.assertEqual(code, 200, payload)
            report = payload["report"]
            excluded = report["payload"]["excluded_samples"]
            self.assertGreaterEqual(excluded["count"], 2)
            self.assertGreaterEqual(excluded["by_metric"].get("accel_util_avg", 0), 1)
            self.assertGreaterEqual(excluded["by_metric"].get("cpu_percent", 0), 1)
            self.assertTrue(any(h["host_id"] == "center-95" for h in excluded["hosts"]))
            ranking = report["payload"]["rankings"][0]
            self.assertEqual(ranking["host_id"], "center-95")
            self.assertEqual(ranking["accel_avg"], 10.0)
            self.assertEqual(ranking["cpu_avg"], 20.0)
            self.assertLess(ranking["accel_avg"], 100)
            md = report["markdown"]
            self.assertIn("已排除超出 0～100% 的利用率样本", md)
            self.assertIn("10.9.9.9", md)
            self.assertIn("加速卡", md)
            self.assertNotIn("1283784", md)
        finally:
            tmp.cleanup()


class ReportConfigAndRetentionTests(unittest.TestCase):
    def test_missing_schedule_keeps_token_and_retention(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "center.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"token": "keep-me", "retention_days": 40, "port": 8080}, fh)
            cfg = load_config(path)
            self.assertEqual(cfg["token"], "keep-me")
            self.assertEqual(cfg["retention_days"], 40)
            self.assertTrue(cfg["report_schedule"]["enabled"])
            self.assertEqual(cfg["report_schedule"]["daily"]["hour"], 0)
            self.assertEqual(cfg["report_schedule"]["daily"]["minute"], 10)
            self.assertEqual(cfg["report_schedule"]["weekly"]["minute"], 20)
            self.assertTrue(cfg["report_schedule"]["weekly"]["enabled"])
            self.assertEqual(cfg["report_schedule"]["monthly"]["minute"], 30)
            ok, errors = validate_center_config(cfg)
            self.assertTrue(ok, errors)

            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "token": "t2",
                        "retention_days": 40,
                        "port": 8080,
                        "report_schedule": {
                            "enabled": False,
                            "daily": {"enabled": False, "hour": 3, "minute": 5},
                        },
                    },
                    fh,
                )
            cfg = load_config(path)
            self.assertEqual(cfg["token"], "t2")
            self.assertEqual(cfg["retention_days"], 40)
            self.assertFalse(cfg["report_schedule"]["enabled"])
            self.assertFalse(cfg["report_schedule"]["daily"]["enabled"])
            self.assertEqual(cfg["report_schedule"]["daily"]["hour"], 3)
            self.assertEqual(cfg["report_schedule"]["weekly"]["minute"], 20)

            with open(path, "w", encoding="utf-8") as fh:
                json.dump(
                    {"port": 8080, "report_schedule": {"daily": {"hour": 99}}},
                    fh,
                )
            cfg = load_config(path)
            ok, errors = validate_center_config(cfg)
            self.assertFalse(ok)
            self.assertTrue(any("daily.hour" in err for err in errors))
        finally:
            tmp.cleanup()

    def test_migration_and_cleanup_keep_reports(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            path = os.path.join(tmp.name, "old.db")
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE diagnostic_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    period_type TEXT NOT NULL,
                    title TEXT NOT NULL,
                    requested_from_ts INTEGER NOT NULL,
                    requested_to_ts INTEGER NOT NULL,
                    clamped INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    markdown TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO diagnostic_reports (
                    period_type, title, requested_from_ts, requested_to_ts,
                    clamped, created_at, markdown, payload
                ) VALUES ('day', '旧报告', 1, 2, 0, 111, 'KEEP_ME', '{}')
                """
            )
            conn.commit()
            conn.close()
            storage = Storage(path, offline_seconds=90, retention_days=7)
            row = storage.get_diagnostic_report(1)
            self.assertEqual(row["markdown"], "KEEP_ME")
            self.assertEqual(row["source"], "manual")
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["generated_at"], 111)
            _post(storage, "h1", int(time.time()) - 10 * 86400, 10, primary_ip="10.0.0.8")
            deleted = storage.cleanup_old_metrics()
            self.assertGreaterEqual(deleted, 1)
            self.assertEqual(storage.get_diagnostic_report(1)["markdown"], "KEEP_ME")
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
