#!/usr/bin/env python3
"""主机资产信息、按 IP 导入、隐藏/恢复/硬删除，以及诊断报告里的负责人/用途。"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center import reports as reports_mod
from center.deploy_store import DeployStore
from center.hostmeta import split_disk_or_model
from center.reports import SHANGHAI, report_period_bounds
from center.storage import Storage
from center.xlsx_util import build_xlsx


FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "hosts-meta-anonymized.csv"
)
SECRET = "CREDENTIAL-SHOULD-NOT-LEAK-9f3a"


def _post(storage: Storage, host_id: str, ts: int, cpu: float = 10, **extra) -> None:
    system = {
        "cpu_percent": cpu,
        "cpu_count": extra.get("cpu_count", 8),
        "cpu_model": extra.get("cpu_model", "Intel Xeon Gold 0000"),
        "mem_percent": extra.get("mem_percent", 20),
        "mem_total_mb": extra.get("mem_total_mb", 16384),
        "disk_percent": extra.get("disk_percent", 10),
        "disk_total_gb": extra.get("disk_total_gb", 200),
        "load1": 0.2,
        "primary_ip": extra.get("primary_ip"),
    }
    if extra.get("disks") is not None:
        system["disks"] = extra["disks"]
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


class HostMetaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=40)
        self.now = int(time.time())
        _post(
            self.storage,
            "host-10-1-0-11",
            self.now,
            primary_ip="10.1.0.11",
            cpu_model="Intel Xeon Silver",
            cpu_count=8,
            mem_total_mb=16384,
            disks=[{"mount": "/", "total_gb": 200, "percent": 10}],
        )
        _post(
            self.storage,
            "host-10-1-0-64",
            self.now,
            primary_ip="10.1.0.64",
            gpus=[{"index": 0, "name": "Example 3090", "vendor": "nvidia", "util_percent": 1}],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_asset_crud_survives_agent_report(self) -> None:
        body = json.dumps(
            {
                "owner": "甲",
                "purpose": "开发环境",
                "cpu": "表格 CPU 8 核",
                "model": "DEMO-001",
                "notes": "手工备注",
            }
        ).encode("utf-8")
        code, saved = api_mod.handle_host_meta_update(
            self.storage, "host-10-1-0-11", body
        )
        self.assertEqual(code, 200, saved)
        self.assertEqual(saved["asset"]["owner"], "甲")

        code, rejected = api_mod.handle_host_meta_update(
            self.storage,
            "host-10-1-0-11",
            json.dumps({"owner": "不该写入", "password": SECRET}).encode("utf-8"),
        )
        self.assertEqual(code, 400, rejected)
        detail_mid = self.storage.get_host("host-10-1-0-11")
        self.assertEqual(detail_mid["asset"]["owner"], "甲")
        self.assertNotIn(SECRET, json.dumps(detail_mid["asset"], ensure_ascii=False))

        _post(
            self.storage,
            "host-10-1-0-11",
            self.now + 5,
            primary_ip="10.1.0.11",
            cpu_model="AMD EPYC",
            cpu_count=32,
            mem_total_mb=65536,
        )
        code, detail = api_mod.handle_host_detail(self.storage, "host-10-1-0-11")
        self.assertEqual(code, 200)
        self.assertEqual(detail["asset"]["owner"], "甲")
        self.assertEqual(detail["asset"]["cpu"], "表格 CPU 8 核")
        self.assertEqual(detail["asset"]["model"], "DEMO-001")
        self.assertEqual(detail["hardware"]["cpu_model"], "AMD EPYC")
        self.assertEqual(detail["hardware"]["cpu_count"], 32)
        self.assertEqual(detail["hardware"]["mem_total_mb"], 65536)
        self.assertEqual(detail["owner"], "甲")
        self.assertEqual(detail["purpose"], "开发环境")

        code, cleared = api_mod.handle_host_meta_update(
            self.storage,
            "host-10-1-0-11",
            json.dumps({"notes": ""}).encode("utf-8"),
        )
        self.assertEqual(code, 200, cleared)
        self.assertEqual(cleared["asset"]["notes"], "")
        self.assertEqual(cleared["asset"]["owner"], "甲")

    def test_import_preview_apply_ignores_credentials(self) -> None:
        with open(FIXTURE, "rb") as fh:
            raw = fh.read()
        self.assertIn(SECRET.encode("utf-8"), raw)
        code, preview = api_mod.handle_host_meta_import(self.storage, raw)
        self.assertEqual(code, 200, preview)
        self.assertFalse(preview["apply"])
        blob = json.dumps(preview, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertIn("密码", preview["ignored_columns"])
        self.assertIn("用户名", preview["ignored_columns"])
        summary = preview["summary"]
        self.assertEqual(summary["matched"], 2)
        self.assertEqual(summary["unmatched"], 1)
        self.assertEqual(summary["skipped"], 1)
        self.assertGreaterEqual(summary["will_update"], 2)
        by_ip = {row["ip"]: row for row in preview["matched"]}
        self.assertEqual(by_ip["10.1.0.11"]["host_id"], "host-10-1-0-11")
        self.assertEqual(by_ip["10.1.0.11"]["patch"]["disk"], "216.22 GB")
        self.assertNotIn("model", by_ip["10.1.0.11"]["patch"])
        self.assertEqual(by_ip["10.1.0.64"]["patch"]["owner"], "乙")
        self.assertIn("DEMO-001", by_ip["10.1.0.64"]["patch"]["model"])
        self.assertNotIn("disk", by_ip["10.1.0.64"]["patch"])

        # 预览不写库
        self.assertEqual(self.storage.get_host("host-10-1-0-11")["owner"], "")

        code, applied = api_mod.handle_host_meta_import(
            self.storage, raw, query="apply=1"
        )
        self.assertEqual(code, 200, applied)
        self.assertTrue(applied["apply"])
        self.assertNotIn(SECRET, json.dumps(applied, ensure_ascii=False))
        host = self.storage.get_host("host-10-1-0-11")
        self.assertEqual(host["asset"]["owner"], "甲")
        self.assertEqual(host["asset"]["purpose"], "开发环境")
        self.assertEqual(host["asset"]["disk"], "216.22 GB")
        self.assertEqual(host["asset"]["memory"], "16 GB")
        self.assertEqual(host["asset"]["monitor_flag"], "是")
        other = self.storage.get_host("host-10-1-0-64")
        self.assertEqual(other["asset"]["owner"], "乙")
        self.assertIn("DEMO-001", other["asset"]["model"])
        conn = sqlite3.connect(self.db)
        try:
            stored = " ".join(
                row[0] or ""
                for row in conn.execute("SELECT asset_meta FROM hosts")
            )
        finally:
            conn.close()
        self.assertNotIn(SECRET, stored)
        self.assertIsNone(self.storage.get_host("10.9.9.9"))

        # 空单元格不覆盖已有负责人
        sparse = (
            "IP地址,负责人,用途\n"
            "10.1.0.11,,只改用途\n"
        ).encode("utf-8")
        code, again = api_mod.handle_host_meta_import(
            self.storage, sparse, query="apply=1"
        )
        self.assertEqual(code, 200, again)
        host = self.storage.get_host("host-10-1-0-11")
        self.assertEqual(host["asset"]["owner"], "甲")
        self.assertEqual(host["asset"]["purpose"], "只改用途")

    def test_xlsx_and_ambiguous_and_duplicate_ip(self) -> None:
        data = build_xlsx(
            [
                ["IP地址", "负责人", "用途", "密码"],
                ["10.1.0.11", "表内甲", "xlsx 用途", SECRET],
            ]
        )
        code, preview = api_mod.handle_host_meta_import(self.storage, data)
        self.assertEqual(code, 200, preview)
        self.assertIn("密码", preview["ignored_columns"])
        self.assertNotIn(SECRET, json.dumps(preview, ensure_ascii=False))
        code, applied = api_mod.handle_host_meta_import(
            self.storage, data, query="apply=1"
        )
        self.assertEqual(code, 200, applied)
        self.assertEqual(
            self.storage.get_host("host-10-1-0-11")["asset"]["purpose"], "xlsx 用途"
        )

        dup = (
            "IP地址,负责人\n"
            "10.1.0.64,先写\n"
            "10.1.0.64,后写\n"
        ).encode("utf-8")
        code, planned = api_mod.handle_host_meta_import(
            self.storage, dup, query="apply=1"
        )
        self.assertEqual(code, 200, planned)
        self.assertEqual(
            self.storage.get_host("host-10-1-0-64")["asset"]["owner"], "后写"
        )
        skipped = [row for row in planned["matched"] if row.get("skipped_apply")]
        self.assertTrue(skipped)

        _post(self.storage, "other-box", self.now, primary_ip="10.1.0.11")
        code, amb = api_mod.handle_host_meta_import(
            self.storage,
            "IP地址,用途\n10.1.0.11,不该写入\n".encode("utf-8"),
            query="apply=1",
        )
        self.assertEqual(code, 200, amb)
        self.assertEqual(amb["summary"]["ambiguous"], 1)
        self.assertEqual(amb["summary"]["will_update"], 0)
        self.assertNotEqual(
            self.storage.get_host("host-10-1-0-11")["asset"]["purpose"], "不该写入"
        )

    def test_hide_restore_delete_and_stats(self) -> None:
        deploy = DeployStore(self.db)
        deploy.add_target(
            {
                "ip": "10.1.0.11",
                "host_id": "host-10-1-0-11",
                "hostname": "vm-demo-a",
                "ssh_user": "root",
            }
        )
        deploy.add_target(
            {
                "ip": "10.9.9.64",
                "host_id": "host-10-9-9-64",
                "hostname": "replacement",
                "ssh_user": "root",
            }
        )
        before = self.storage.cluster_stats()
        self.assertEqual(before["host_total"], 2)
        self.assertEqual(before["host_online"], 2)

        code, hidden = api_mod.handle_host_hide(
            self.storage,
            "host-10-1-0-11",
            b"{}",
            deploy_store=deploy,
        )
        self.assertEqual(code, 200, hidden)
        self.assertEqual(hidden["removed_deploy_targets"], [])
        self.assertEqual(len(deploy.list_targets()), 2)

        listed = api_mod.handle_hosts_list(self.storage)[1]["hosts"]
        self.assertEqual([h["host_id"] for h in listed], ["host-10-1-0-64"])
        hidden_only = api_mod.handle_hosts_list(self.storage, query="hidden=1")[1][
            "hosts"
        ]
        self.assertEqual([h["host_id"] for h in hidden_only], ["host-10-1-0-11"])
        stats = self.storage.cluster_stats()
        self.assertEqual(stats["host_total"], 1)
        self.assertEqual(stats["host_online"], 1)
        period = self.storage.cluster_period_stats(minutes=60)
        self.assertNotIn(
            "host-10-1-0-11", [h["host_id"] for h in period["hosts"]]
        )
        _, anomaly = api_mod.handle_anomaly(self.storage)
        blob = json.dumps(anomaly, ensure_ascii=False)
        self.assertNotIn("host-10-1-0-11", blob)
        _, csv_text = api_mod.handle_export_csv(self.storage)
        self.assertNotIn("host-10-1-0-11", csv_text)
        self.assertIn("owner", csv_text.splitlines()[0])

        # 再上报也不会自动回到列表
        _post(self.storage, "host-10-1-0-11", self.now + 10, primary_ip="10.1.0.11")
        listed = api_mod.handle_hosts_list(self.storage)[1]["hosts"]
        self.assertEqual([h["host_id"] for h in listed], ["host-10-1-0-64"])
        self.assertIsNotNone(self.storage.get_host("host-10-1-0-11"))

        code, restored = api_mod.handle_host_restore(
            self.storage, "host-10-1-0-11"
        )
        self.assertEqual(code, 200, restored)
        self.assertEqual(self.storage.cluster_stats()["host_total"], 2)

        code, hidden = api_mod.handle_host_hide(
            self.storage,
            "host-10-1-0-11",
            json.dumps({"remove_deploy_target": True}).encode("utf-8"),
            deploy_store=deploy,
        )
        self.assertEqual(code, 200, hidden)
        self.assertEqual(len(hidden["removed_deploy_targets"]), 1)
        left = {(t["ip"], t["host_id"]) for t in deploy.list_targets()}
        self.assertNotIn(("10.1.0.11", "host-10-1-0-11"), left)
        self.assertIn(("10.9.9.64", "host-10-9-9-64"), left)

        code, denied = api_mod.handle_host_delete(
            self.storage, "host-10-1-0-11", query=""
        )
        self.assertEqual(code, 400, denied)
        self.assertIsNotNone(self.storage.get_host("host-10-1-0-11"))
        conn = sqlite3.connect(self.db)
        try:
            hist = conn.execute(
                "SELECT COUNT(*) FROM metrics_history WHERE host_id=?",
                ("host-10-1-0-11",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertGreater(hist, 0)

        code, purged = api_mod.handle_host_delete(
            self.storage, "host-10-1-0-11", query="confirm=delete"
        )
        self.assertEqual(code, 200, purged)
        self.assertGreater(purged["history_rows"], 0)
        self.assertIsNone(self.storage.get_host("host-10-1-0-11"))
        conn = sqlite3.connect(self.db)
        try:
            hist = conn.execute(
                "SELECT COUNT(*) FROM metrics_history WHERE host_id=?",
                ("host-10-1-0-11",),
            ).fetchone()[0]
            still = conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(hist, 0)
        self.assertEqual(still, 1)
        self.assertEqual(self.storage.cluster_stats()["host_total"], 1)

        _post(self.storage, "host-10-1-0-11", self.now + 20, primary_ip="10.1.0.11")
        fresh = self.storage.get_host("host-10-1-0-11")
        self.assertEqual(fresh["asset"]["owner"], "")
        self.assertFalse(fresh["hidden"])

    def test_legacy_table_gains_columns_without_wiping_rows(self) -> None:
        path = os.path.join(self.tmp.name, "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE hosts (
                host_id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                host_type TEXT NOT NULL,
                last_seen INTEGER NOT NULL,
                last_payload TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE metrics_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host_id TEXT NOT NULL,
                ts INTEGER NOT NULL,
                payload TEXT NOT NULL
            );
            """
        )
        payload = json.dumps(
            {"host_id": "old", "system": {"cpu_percent": 3, "primary_ip": "10.2.0.8"}}
        )
        conn.execute(
            """
            INSERT INTO hosts (host_id, hostname, host_type, last_seen, last_payload, created_at)
            VALUES ('old', 'old', 'cpu', ?, ?, ?)
            """,
            (self.now, payload, self.now),
        )
        conn.execute(
            "INSERT INTO metrics_history (host_id, ts, payload) VALUES ('old', ?, ?)",
            (self.now, payload),
        )
        conn.commit()
        conn.close()

        storage = Storage(path, offline_seconds=90, retention_days=7)
        host = storage.get_host("old")
        self.assertIsNotNone(host)
        self.assertFalse(host["hidden"])
        self.assertEqual(host["asset"]["owner"], "")
        self.assertEqual(storage.cluster_stats()["host_total"], 1)
        conn = sqlite3.connect(path)
        try:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(hosts)")}
            hist = conn.execute("SELECT COUNT(*) FROM metrics_history").fetchone()[0]
        finally:
            conn.close()
        self.assertIn("hidden", cols)
        self.assertIn("hidden_at", cols)
        self.assertIn("asset_meta", cols)
        self.assertIn("last_remote_ip", cols)
        self.assertEqual(hist, 1)

    def test_report_includes_metadata_and_skips_hidden(self) -> None:
        bounds = report_period_bounds("day", self.now)
        span = bounds["to_ts"] - bounds["from_ts"]
        samples = [bounds["from_ts"] + int((span - 2) * i / 12) for i in range(12)]
        for ts in samples:
            _post(self.storage, "idle-box", ts, 1, primary_ip="10.4.0.3")
            _post(self.storage, "gone-box", ts, 1, primary_ip="10.4.0.9")
        _post(self.storage, "idle-box", self.now, 1, primary_ip="10.4.0.3")
        api_mod.handle_host_meta_update(
            self.storage,
            "idle-box",
            json.dumps({"owner": "甲", "purpose": "可回收的开发机"}).encode("utf-8"),
        )
        api_mod.handle_host_meta_update(
            self.storage,
            "gone-box",
            json.dumps({"owner": "已走的人", "purpose": "旧机器"}).encode("utf-8"),
        )
        api_mod.handle_host_hide(self.storage, "gone-box", b"{}")

        code, payload = reports_mod.handle_generate_report(
            self.storage,
            json.dumps({"period": "day", "as_of": self.now}).encode("utf-8"),
            busy_thresholds={"cpu_percent": 80.0, "accel_util_avg": 80.0},
        )
        self.assertEqual(code, 200, payload)
        md = payload["report"]["markdown"]
        self.assertIn("甲", md)
        self.assertIn("可回收的开发机", md)
        self.assertIn("负责人", md)
        self.assertIn("用途", md)
        self.assertNotIn("gone-box", md)
        self.assertNotIn("已走的人", md)
        self.assertNotIn("10.4.0.9", md)
        rankings = payload["report"]["payload"]["rankings"]
        ids = [row["host_id"] for row in rankings]
        self.assertIn("idle-box", ids)
        self.assertNotIn("gone-box", ids)
        idle = next(row for row in rankings if row["host_id"] == "idle-box")
        self.assertEqual(idle["owner"], "甲")
        self.assertEqual(idle["purpose"], "可回收的开发机")
        self.assertEqual(idle["pole"], "idle")
        code, csv_payload = reports_mod.handle_report_csv(
            self.storage, payload["report"]["id"]
        )
        header = csv_payload["csv"].splitlines()[0]
        self.assertIn("owner", header)
        self.assertIn("purpose", header)
        self.assertIn("甲", csv_payload["csv"])
        self.assertNotIn("gone-box", csv_payload["csv"])


class UnitEnvTests(unittest.TestCase):
    def test_preserves_custom_environment_lines(self) -> None:
        script = os.path.join(ROOT, "scripts", "lib", "unit_env.sh")
        with tempfile.TemporaryDirectory() as tmp:
            unit = os.path.join(tmp, "monitor-center.service")
            with open(unit, "w", encoding="utf-8") as fh:
                fh.write(
                    "\n".join(
                        [
                            "[Service]",
                            "Environment=PYTHONPATH=/old/monitor",
                            "Environment=PATH=/usr/local/python3/bin:/usr/bin",
                            "Environment=LD_LIBRARY_PATH=/old/lib",
                            "Environment=FOO=bar",
                            "",
                        ]
                    )
                )
            out = subprocess.check_output(
                [
                    "bash",
                    "-c",
                    'source "$1"; collect_preserved_environment "$2" /opt/monitor /new/lib',
                    "bash",
                    script,
                    unit,
                ],
                text=True,
            )
            lines = [line for line in out.splitlines() if line.strip()]
            self.assertIn("Environment=PYTHONPATH=/opt/monitor", lines)
            self.assertIn("Environment=LD_LIBRARY_PATH=/new/lib", lines)
            self.assertIn("Environment=PATH=/usr/local/python3/bin:/usr/bin", lines)
            self.assertIn("Environment=FOO=bar", lines)
            self.assertNotIn("Environment=PYTHONPATH=/old/monitor", lines)
            self.assertNotIn("Environment=LD_LIBRARY_PATH=/old/lib", lines)

            kept = subprocess.check_output(
                [
                    "bash",
                    "-c",
                    'source "$1"; collect_preserved_environment "$2" /opt/monitor ""',
                    "bash",
                    script,
                    unit,
                ],
                text=True,
            )
            self.assertIn("Environment=LD_LIBRARY_PATH=/old/lib", kept)
            self.assertIn("Environment=PATH=/usr/local/python3/bin:/usr/bin", kept)
            self.assertNotIn("Environment=PYTHONPATH=/old/monitor", kept)


class SplitDiskModelTests(unittest.TestCase):
    def test_capacity_versus_free_text(self) -> None:
        self.assertEqual(split_disk_or_model("216.22 GB"), ("216.22 GB", ""))
        self.assertEqual(split_disk_or_model("500G"), ("500G", ""))
        disk, model = split_disk_or_model("示例服务器 EIS-2208、资产号 DEMO-001")
        self.assertEqual(disk, "")
        self.assertIn("DEMO-001", model)


if __name__ == "__main__":
    unittest.main()
