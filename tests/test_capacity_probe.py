#!/usr/bin/env python3
"""容量探针：外推公式、冒烟不宣称上限、拒绝现场目录。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import capacity_probe as probe  # noqa: E402


class ProjectionTests(unittest.TestCase):
    def test_samples_per_day_rounds_up(self) -> None:
        self.assertEqual(probe.samples_per_host_per_day(15), 5760)
        self.assertEqual(probe.samples_per_host_per_day(10), 8640)
        self.assertEqual(probe.samples_per_host_per_day(86401), 1)

    def test_rejects_bad_knobs(self) -> None:
        with self.assertRaises(ValueError):
            probe.samples_per_host_per_day(0)
        with self.assertRaises(ValueError):
            probe.samples_per_host_per_day(-5)
        with self.assertRaises(ValueError):
            probe.projected_history_rows(0, 15, 7)
        with self.assertRaises(ValueError):
            probe.projected_history_rows(2, 15, 0)
        with self.assertRaises(ValueError):
            probe.projected_bytes(-1, 10)
        with self.assertRaises(ValueError):
            probe.projected_bytes(10, -1)

    def test_retention_projection_is_rows_not_a_host_cap(self) -> None:
        rows = probe.projected_history_rows(2, 15, 7)
        self.assertEqual(rows, 2 * 5760 * 7)
        self.assertEqual(probe.projected_bytes(rows, 10), rows * 10)
        self.assertEqual(probe.projected_bytes(10, 100.4), 1004)


class ProbeRunTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="capprobe-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_smoke_makes_no_capacity_claim(self) -> None:
        work = os.path.join(self.tmp, "lab")
        result = probe.run_probe(
            hosts=probe.SMOKE["hosts"],
            interval_seconds=probe.SMOKE["interval_seconds"],
            retention_days=probe.SMOKE["retention_days"],
            samples_per_host=probe.SMOKE["samples_per_host"],
            work_dir=work,
            mode="smoke",
        )
        self.assertTrue(result["laboratory_estimate"])
        self.assertFalse(result["sla"])
        self.assertFalse(result["capacity_claim"])
        self.assertIsNone(result["published_host_limit"])
        self.assertEqual(result["dual_center"], "deferred")
        self.assertFalse(result["fill_results_table"])
        self.assertEqual(result["measured"]["history_rows"], 4)
        self.assertEqual(result["measured"]["diagnostic_reports"], 0)
        self.assertEqual(result["measured"]["hosts_seen"], 2)
        self.assertGreater(result["measured"]["bytes_per_history_row"], 0)
        self.assertEqual(result["projection"]["history_rows_at_retention"], 2 * 5760 * 7)
        text = "\n".join(result["warnings"]) + "\n" + probe.format_report(result)
        self.assertIn("不是 SLA", text)
        self.assertIn("没有主机上限", text)
        self.assertIn("不要填进", probe.format_report(result))
        self.assertNotIn("能撑", probe.format_report(result))

    def test_sample_run_records_reads_and_refuses_existing_db(self) -> None:
        work = os.path.join(self.tmp, "sample")
        result = probe.run_probe(
            hosts=3,
            interval_seconds=15,
            retention_days=40,
            samples_per_host=4,
            work_dir=work,
            mode="sample",
        )
        self.assertTrue(result["fill_results_table"])
        self.assertEqual(result["measured"]["history_rows"], 12)
        self.assertEqual(result["measured"]["diagnostic_reports"], 0)
        self.assertEqual(result["projection"]["history_rows_at_retention"], 3 * 5760 * 40)
        self.assertIsNotNone(result["measured"]["ingest_ms"]["p95"])
        self.assertGreater(result["measured"]["reads_ms"]["cluster_period"], 0)
        with self.assertRaises(probe.CapacityError):
            probe.run_probe(
                hosts=1,
                interval_seconds=15,
                retention_days=7,
                samples_per_host=1,
                work_dir=work,
                mode="sample",
            )

    def test_http_uses_metrics_api(self) -> None:
        work = os.path.join(self.tmp, "http")
        result = probe.run_probe(
            hosts=1,
            interval_seconds=15,
            retention_days=7,
            samples_per_host=1,
            work_dir=work,
            http=True,
            mode="sample",
        )
        self.assertEqual(result["transport"], "http")
        self.assertEqual(result["measured"]["hosts_seen"], 1)
        self.assertEqual(result["measured"]["history_rows"], 1)
        self.assertEqual(result["measured"]["diagnostic_reports"], 0)

    def test_max_writes_is_a_lab_guard_not_a_limit(self) -> None:
        work = os.path.join(self.tmp, "cap")
        with self.assertRaises(probe.CapacityError) as ctx:
            probe.run_probe(
                hosts=5,
                interval_seconds=15,
                retention_days=7,
                samples_per_host=5,
                work_dir=work,
                max_writes=10,
            )
        self.assertIn("不是容量结论", str(ctx.exception))
        self.assertFalse(os.path.exists(os.path.join(work, "data", "monitor.db")))

    def test_refuses_live_install_paths(self) -> None:
        with self.assertRaises(probe.CapacityError):
            probe.assert_lab_dir("/opt/monitor")
        with self.assertRaises(probe.CapacityError):
            probe.assert_lab_dir("/opt/monitor/data")
        with self.assertRaises(probe.CapacityError):
            probe.assert_lab_dir(os.path.expanduser("~/monitor"))
        # 升级备份目录不是现场安装树，不能被 /opt/monitor 前缀误伤。
        self.assertTrue(probe.assert_lab_dir("/opt/monitor-backup-20200101010101").endswith(
            "monitor-backup-20200101010101"
        ))
        live = os.path.join(self.tmp, "looks-live")
        os.makedirs(os.path.join(live, "config"))
        with open(os.path.join(live, "config", "center.json"), "w", encoding="utf-8") as fh:
            fh.write("{}")
        with self.assertRaises(probe.CapacityError):
            probe.assert_lab_dir(live)


class ProbeCliTests(unittest.TestCase):
    def test_smoke_cli(self) -> None:
        proc = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "capacity_probe.py"), "--smoke"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("不是 SLA", proc.stdout)
        self.assertIn("冒烟结果不要填进", proc.stdout)
        self.assertNotIn("published_host_limit", proc.stdout)


class PercentileTests(unittest.TestCase):
    def test_percentile_ms(self) -> None:
        self.assertEqual(probe.percentile_ms([1.0, 2.0, 3.0, 4.0], 95), 3.85)
        self.assertIsNone(probe.percentile_ms([], 50))


if __name__ == "__main__":
    unittest.main()
