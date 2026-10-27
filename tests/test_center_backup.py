#!/usr/bin/env python3
"""中心备份 / 恢复：同一布局、代码回滚不动数据库、显式 --with-data 才覆盖库。"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS = os.path.join(ROOT, "scripts")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if SCRIPTS not in sys.path:
    sys.path.insert(0, SCRIPTS)

import center_backup as backup  # noqa: E402
from center.storage import Storage  # noqa: E402


def _text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _payload(host_id: str, ts: int) -> dict:
    return {
        "host_id": host_id,
        "hostname": host_id,
        "host_type": "cpu",
        "timestamp": ts,
        "system": {"cpu_percent": 3, "mem_percent": 4, "disk_percent": 5, "load1": 0.1},
    }


class BackupRestoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="center-backup-")
        self.install = os.path.join(self.tmp, "monitor")
        os.makedirs(os.path.join(self.install, "config"))
        os.makedirs(os.path.join(self.install, "center"))
        os.makedirs(os.path.join(self.install, "vendor", "python"))
        self.cfg = {
            "token": "keep-token",
            "retention_days": 9,
            "port": 8080,
        }
        with open(os.path.join(self.install, "config", "center.json"), "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh)
        with open(os.path.join(self.install, "center", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write('__version__ = "old-code"\n')
        self.bundle = os.path.join(self.install, "vendor", "python", "fake.tar.gz")
        with open(self.bundle, "wb") as fh:
            fh.write(b"bundle-bytes")
        self.db = os.path.join(self.install, "data", "monitor.db")
        storage = Storage(self.db, offline_seconds=90, retention_days=9)
        storage.upsert_metric(_payload("live-1", 1_700_000_000))
        with open(os.path.join(self.install, "data", "note.txt"), "w", encoding="utf-8") as fh:
            fh.write("leave-me")
        self.unit = os.path.join(self.tmp, "monitor-center.service")
        with open(self.unit, "w", encoding="utf-8") as fh:
            fh.write(
                textwrap.dedent(
                    """\
                    [Service]
                    Environment=PYTHONPATH=/opt/monitor
                    Environment=PATH=/custom/bin
                    Environment=LD_LIBRARY_PATH=/custom/lib
                    ExecStart=/usr/bin/python3 -m center --config /opt/monitor/config/center.json
                    """
                )
            )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _counts(self) -> int:
        conn = sqlite3.connect(self.db)
        try:
            return int(conn.execute("SELECT COUNT(*) FROM metrics_history").fetchone()[0])
        finally:
            conn.close()

    def test_backup_matches_upgrade_layout_and_leaves_live_data(self) -> None:
        before = self._counts()
        manifest = backup.create_backup(self.install, stamp="20200101010101", unit_path=self.unit)
        dest = manifest["backup_dir"]
        self.assertTrue(os.path.basename(dest).startswith("monitor-backup-20200101010101"))
        self.assertTrue(os.path.isfile(os.path.join(dest, "code", "config", "center.json")))
        self.assertTrue(os.path.isfile(os.path.join(dest, "code", "data", "monitor.db")))
        self.assertFalse(os.path.exists(os.path.join(dest, "code", "data", "monitor.db-wal")))
        self.assertEqual(
            _bytes(os.path.join(dest, "code", "vendor", "python", "fake.tar.gz")),
            b"bundle-bytes",
        )
        unit = _text(os.path.join(dest, "monitor-center.service"))
        self.assertIn("Environment=PATH=/custom/bin", unit)
        self.assertIn("Environment=LD_LIBRARY_PATH=/custom/lib", unit)
        backup.quick_check(os.path.join(dest, "code", "data", "monitor.db"))
        self.assertEqual(self._counts(), before)
        self.assertEqual(manifest["kind"], backup.BACKUP_KIND)
        self.assertTrue(manifest["preserved"]["database"])
        self.assertTrue(manifest["preserved"]["vendor"])
        self.assertTrue(manifest["preserved"]["unit_file"])
        self.assertIn("不是双中心", "\n".join(manifest["notes"]))

    def test_code_only_restores_code_config_vendor_unit_not_database(self) -> None:
        manifest = backup.create_backup(self.install, stamp="20200101010101", unit_path=self.unit)
        dest = manifest["backup_dir"]
        with open(os.path.join(self.install, "center", "__init__.py"), "w", encoding="utf-8") as fh:
            fh.write('__version__ = "new-code"\n')
        with open(os.path.join(self.install, "center", "extra.txt"), "w", encoding="utf-8") as fh:
            fh.write("only-live")
        os.remove(self.bundle)
        self.cfg["token"] = "changed"
        with open(os.path.join(self.install, "config", "center.json"), "w", encoding="utf-8") as fh:
            json.dump(self.cfg, fh)
        Storage(self.db, offline_seconds=90, retention_days=9).upsert_metric(
            _payload("live-2", 1_700_000_100)
        )
        self.assertEqual(self._counts(), 2)
        with open(self.unit, "w", encoding="utf-8") as fh:
            fh.write("Environment=PATH=/wiped\n")
        # 升级产生的备份没有 manifest，恢复仍要认这个布局。
        os.remove(os.path.join(dest, "backup-manifest.json"))
        plan = backup.describe_restore("code-only")
        self.assertFalse(plan["overwrites_database"])
        result = backup.restore_center(
            self.install,
            dest,
            "code-only",
            yes=True,
            unit_path=self.unit,
        )
        self.assertFalse(result["database_restored"])
        self.assertTrue(result["unit_restored"])
        self.assertIn(
            '__version__ = "old-code"',
            _text(os.path.join(self.install, "center", "__init__.py")),
        )
        self.assertFalse(os.path.exists(os.path.join(self.install, "center", "extra.txt")))
        self.assertEqual(_bytes(self.bundle), b"bundle-bytes")
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["token"], "keep-token")
        self.assertEqual(self._counts(), 2)
        self.assertEqual(_text(os.path.join(self.install, "data", "note.txt")), "leave-me")
        restored_unit = _text(self.unit)
        self.assertIn("Environment=PATH=/custom/bin", restored_unit)
        self.assertIn("Environment=LD_LIBRARY_PATH=/custom/lib", restored_unit)
        again = backup.restore_center(
            self.install, dest, "code-only", yes=True, unit_path=self.unit
        )
        self.assertTrue(again["ok"])
        self.assertEqual(self._counts(), 2)

    def test_with_data_requires_stopped_and_replaces_db(self) -> None:
        manifest = backup.create_backup(self.install, stamp="20200101020202", unit_path=self.unit)
        dest = manifest["backup_dir"]
        Storage(self.db, offline_seconds=90, retention_days=9).upsert_metric(
            _payload("live-2", 1_700_000_100)
        )
        with self.assertRaises(backup.BackupError) as ctx:
            backup.restore_center(self.install, dest, "with-data", yes=True, assume_stopped=False)
        self.assertIn("assume-stopped", str(ctx.exception))
        self.assertEqual(self._counts(), 2)
        with self.assertRaises(backup.BackupError):
            backup.restore_center(
                self.install, dest, "with-data", yes=False, assume_stopped=True
            )
        self.assertEqual(self._counts(), 2)
        result = backup.restore_center(
            self.install,
            dest,
            "with-data",
            yes=True,
            assume_stopped=True,
            unit_path=self.unit,
        )
        self.assertTrue(result["database_restored"])
        self.assertEqual(self._counts(), 1)
        self.assertEqual(_text(os.path.join(self.install, "data", "note.txt")), "leave-me")
        backup.quick_check(self.db)
        again = backup.restore_center(
            self.install, dest, "with-data", yes=True, assume_stopped=True, unit_path=self.unit
        )
        self.assertEqual(again["counts"]["metrics_history"], 1)

    def test_corrupt_backup_db_does_not_touch_live(self) -> None:
        manifest = backup.create_backup(self.install, stamp="20200101030303")
        dest = manifest["backup_dir"]
        marker = os.path.join(self.install, "center", "__init__.py")
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write("still-live\n")
        db_copy = os.path.join(dest, "code", "data", "monitor.db")
        with open(db_copy, "wb") as fh:
            fh.write(b"not-a-database")
        with self.assertRaises(backup.BackupError):
            backup.restore_center(
                self.install, dest, "with-data", yes=True, assume_stopped=True
            )
        self.assertEqual(_text(marker), "still-live\n")
        self.assertEqual(self._counts(), 1)

    def test_refuses_missing_config_nested_dirs_and_keep_zero(self) -> None:
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty)
        with self.assertRaises(backup.BackupError):
            backup.create_backup(empty, stamp="20200101010101")
        manifest = backup.create_backup(self.install, stamp="20200101040404")
        with self.assertRaises(backup.BackupError):
            backup.restore_center(self.install, self.install, "code-only", yes=True)
        nested = os.path.join(self.install, "monitor-backup-nested")
        shutil.copytree(manifest["backup_dir"], nested)
        with self.assertRaises(backup.BackupError):
            backup.restore_center(self.install, nested, "code-only", yes=True)
        with self.assertRaises(backup.BackupError):
            backup.prune_backups(self.install, 0)

    def test_prune_keeps_newest_shared_names(self) -> None:
        backup.create_backup(self.install, stamp="20200101010101")
        newest = backup.create_backup(self.install, stamp="20200102020202")
        removed = backup.prune_backups(self.install, 1)
        self.assertEqual(len(removed), 1)
        self.assertTrue(removed[0].endswith("monitor-backup-20200101010101"))
        self.assertTrue(os.path.isdir(newest["backup_dir"]))
        self.assertTrue(os.path.isdir(self.install))
        self.assertTrue(os.path.isfile(self.db))


class BackupCliTests(unittest.TestCase):
    def test_shell_backup_and_code_only_restore(self) -> None:
        tmp = tempfile.mkdtemp(prefix="center-backup-cli-")
        try:
            install = os.path.join(tmp, "monitor")
            os.makedirs(os.path.join(install, "config"))
            os.makedirs(os.path.join(install, "center"))
            with open(os.path.join(install, "config", "center.json"), "w", encoding="utf-8") as fh:
                json.dump({"token": "t", "port": 9}, fh)
            code = os.path.join(install, "center", "__init__.py")
            with open(code, "w", encoding="utf-8") as fh:
                fh.write("old\n")
            Storage(os.path.join(install, "data", "monitor.db"), offline_seconds=90, retention_days=7).upsert_metric(
                _payload("h", 1_700_000_000)
            )
            env = os.environ.copy()
            env["PYTHONPATH"] = ROOT
            env["MONITOR_TEST_SANDBOX"] = "1"
            proc = subprocess.run(
                [
                    "bash",
                    os.path.join(SCRIPTS, "backup_center.sh"),
                    "--dir",
                    install,
                    "--no-unit",
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("不是双中心", proc.stdout)
            backups = [
                os.path.join(tmp, name)
                for name in os.listdir(tmp)
                if name.startswith("monitor-backup-")
            ]
            self.assertEqual(len(backups), 1)
            with open(code, "w", encoding="utf-8") as fh:
                fh.write("new\n")
            proc = subprocess.run(
                [
                    "bash",
                    os.path.join(SCRIPTS, "restore_center.sh"),
                    "--dir",
                    install,
                    "--from",
                    backups[0],
                    "--code-only",
                    "--yes",
                    "--no-systemd",
                    "--no-unit",
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("data/ 未改", proc.stdout)
            self.assertEqual(_text(code), "old\n")
            proc = subprocess.run(
                [
                    "bash",
                    os.path.join(SCRIPTS, "restore_center.sh"),
                    "--dir",
                    install,
                    "--from",
                    backups[0],
                    "--with-data",
                    "--yes",
                    "--no-systemd",
                ],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("assume-stopped", proc.stdout)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
