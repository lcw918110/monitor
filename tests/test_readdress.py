#!/usr/bin/env python3
"""同机更换 IP：部署目标身份冲突、展示地址优先级、历史 IP 匹配、改址接口。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center.deploy_api import handle_update_target
from center.deploy_store import DeployStore
from center.hostaddr import resolve_host_address
from center.hostmeta import host_match_ips
from center.readdress import shanghai_today
from center.storage import Storage


def _post(storage: Storage, host_id: str, ts: int, primary_ip: str = "") -> None:
    api_mod.handle_metrics_post(
        storage,
        json.dumps(
            {
                "host_id": host_id,
                "hostname": host_id,
                "host_type": "cpu",
                "timestamp": ts,
                "system": {
                    "cpu_percent": 3,
                    "mem_percent": 4,
                    "disk_percent": 5,
                    "primary_ip": primary_ip,
                },
            }
        ).encode("utf-8"),
    )


class DeployIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeployStore(os.path.join(self.tmp.name, "d.db"))
        self.keep = self.store.add_target(
            {
                "ip": "172.24.5.3",
                "host_id": "host-172-24-5-3",
                "ssh_password": "secret-old",
                "ssh_key_path": "/keys/old",
            }
        )
        self.move = self.store.add_target(
            {
                "ip": "192.168.198.64",
                "host_id": "host-192-168-198-64",
                "ssh_password": "secret-new",
                "ssh_key_path": "/keys/new",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _secret(self, target_id: int) -> dict:
        row = self.store.get_target(target_id, include_secrets=True)
        self.assertIsNotNone(row)
        return row

    def test_update_ip_and_host_id_keeps_credentials(self) -> None:
        updated = self.store.update_target(
            int(self.move["id"]),
            {
                "ip": "192.168.198.65",
                "host_id": "host-172-24-5-3",
                "hostname": "moved",
            },
        )
        self.assertEqual(updated["ip"], "192.168.198.65")
        self.assertEqual(updated["host_id"], "host-172-24-5-3")
        self.assertEqual(updated["hostname"], "moved")
        secret = self._secret(int(self.move["id"]))
        self.assertEqual(secret["ssh_password"], "secret-new")
        self.assertEqual(secret["ssh_key_path"], "/keys/new")
        # 空密码表示保持，不把已保存的密码清掉。
        again = self.store.update_target(
            int(self.move["id"]),
            {"ssh_password": "", "hostname": "moved-2"},
        )
        self.assertEqual(again["hostname"], "moved-2")
        self.assertEqual(self._secret(int(self.move["id"]))["ssh_password"], "secret-new")

    def test_refuses_ip_conflict_and_invalid_values(self) -> None:
        before = self._secret(int(self.move["id"]))
        with self.assertRaises(ValueError) as ctx:
            self.store.update_target(int(self.move["id"]), {"ip": "172.24.5.3"})
        self.assertIn("IP", str(ctx.exception))
        after = self._secret(int(self.move["id"]))
        self.assertEqual(after["ip"], before["ip"])
        self.assertEqual(after["host_id"], before["host_id"])
        self.assertEqual(after["ssh_password"], "secret-new")

        for bad_ip in ("999.1.1.1", "不是地址", "127.0.0.1", ""):
            with self.assertRaises(ValueError):
                self.store.update_target(int(self.move["id"]), {"ip": bad_ip})
        for bad_id in ("", "a/b", "has space"):
            with self.assertRaises(ValueError):
                self.store.update_target(int(self.move["id"]), {"host_id": bad_id})
        unchanged = self._secret(int(self.move["id"]))
        self.assertEqual(unchanged["ip"], "192.168.198.64")
        self.assertEqual(unchanged["host_id"], "host-192-168-198-64")
        self.assertEqual(unchanged["ssh_password"], "secret-new")

        code, resp = handle_update_target(
            self.store,
            int(self.move["id"]),
            b'{"ip":"172.24.5.3","host_id":"host-192-168-198-64"}',
        )
        self.assertEqual(code, 400)
        self.assertFalse(resp["ok"])
        self.assertIn("IP", resp["error"])


class AddressMatchTests(unittest.TestCase):
    def test_manual_address_beats_deploy_and_embedded_ip(self) -> None:
        self.assertEqual(
            resolve_host_address(
                host_id="host-172-24-5-3",
                hostname="host-172-24-5-3",
                payload={"system": {"primary_ip": "10.1.1.1"}},
                deploy_ip="172.24.5.3",
                manual_address="192.168.198.64",
            ),
            "192.168.198.64",
        )

    def test_import_matches_previous_ip(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = Storage(os.path.join(tmp.name, "t.db"), offline_seconds=90, retention_days=7)
        now = int(time.time())
        _post(storage, "gpu-box", now, primary_ip="10.8.8.8")
        err, _asset = storage.set_host_readdress(
            "gpu-box",
            "10.8.8.8",
            [{"ip": "10.9.9.9", "date": "2026-01-02", "note": "旧地址"}],
        )
        self.assertIsNone(err)
        host = storage.get_host("gpu-box")
        self.assertEqual(host["address"], "10.8.8.8")
        self.assertIn("10.9.9.9", host_match_ips(host))
        body = "IP地址,负责人,用途\n10.9.9.9,甲,迁走前的机器\n".encode("utf-8")
        code, preview = api_mod.handle_host_meta_import(storage, body)
        self.assertEqual(code, 200)
        self.assertEqual(preview["summary"]["matched"], 1)
        self.assertEqual(preview["matched"][0]["host_id"], "gpu-box")
        self.assertEqual(storage.get_host("gpu-box")["owner"], "")
        code, applied = api_mod.handle_host_meta_import(storage, body, query="apply=1")
        self.assertEqual(code, 200)
        saved = storage.get_host("gpu-box")
        self.assertEqual(saved["owner"], "甲")
        self.assertEqual(saved["asset"]["address"], "10.8.8.8")
        self.assertEqual(saved["previous_ips"][0]["ip"], "10.9.9.9")

        # 表头「地址」只匹配，不写进手工展示地址。
        other = "地址,负责人\n10.8.8.8,乙\n".encode("utf-8")
        code, applied = api_mod.handle_host_meta_import(storage, other, query="apply=1")
        self.assertEqual(code, 200)
        saved = storage.get_host("gpu-box")
        self.assertEqual(saved["owner"], "乙")
        self.assertEqual(saved["asset"]["address"], "10.8.8.8")


class ReaddressEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=40)
        self.store = DeployStore(self.db)
        now = int(time.time())
        _post(self.storage, "host-172-24-5-3", now - 10, primary_ip="198.18.0.1")
        _post(self.storage, "host-172-24-5-3", now, primary_ip="198.18.0.1")
        self.old = self.store.add_target(
            {
                "ip": "172.24.5.3",
                "host_id": "host-172-24-5-3",
                "ssh_password": "secret-old",
            }
        )
        self.new = self.store.add_target(
            {
                "ip": "192.168.198.64",
                "host_id": "host-192-168-198-64",
                "ssh_password": "secret-new",
            }
        )
        self.history_before = self.storage.history_count("host-172-24-5-3")
        self.assertGreaterEqual(self.history_before, 2)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _call(self, body: dict):
        return api_mod.handle_host_readdress(
            self.storage,
            "host-172-24-5-3",
            json.dumps(body).encode("utf-8"),
            deploy_store=self.store,
        )

    def test_preview_writes_nothing_and_apply_keeps_history(self) -> None:
        shown = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(shown["address"], "172.24.5.3")
        code, preview = self._call(
            {"new_ip": "192.168.198.64", "note": "同机改址到 192.168.198.64"}
        )
        self.assertEqual(code, 200)
        self.assertFalse(preview["apply"])
        self.assertEqual(preview["old_ip"], "172.24.5.3")
        self.assertEqual(preview["new_ip"], "192.168.198.64")
        self.assertEqual(preview["deploy_targets_retargeted"][0]["id"], self.new["id"])
        self.assertEqual(
            preview["deploy_targets_retargeted"][0]["host_id_to"], "host-172-24-5-3"
        )
        self.assertEqual(preview["deploy_targets_retired"][0]["id"], self.old["id"])
        self.assertEqual(self.storage.get_host("host-172-24-5-3")["address"], "172.24.5.3")
        self.assertEqual(
            self.store.get_target(int(self.new["id"]))["host_id"], "host-192-168-198-64"
        )
        self.assertFalse(self.store.get_target(int(self.old["id"]))["retired"])
        self.assertEqual(self.storage.history_count("host-172-24-5-3"), self.history_before)

        code, applied = self._call(
            {
                "new_ip": "192.168.198.64",
                "note": "同机改址到 192.168.198.64",
                "apply": True,
            }
        )
        self.assertEqual(code, 200, applied)
        self.assertTrue(applied["apply"])
        self.assertTrue(applied["host_id_unchanged"])
        self.assertFalse(applied["history_rewritten"])
        self.assertEqual(applied["history_rows"], self.history_before)
        host = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(host["host_id"], "host-172-24-5-3")
        self.assertEqual(host["address"], "192.168.198.64")
        self.assertEqual(host["previous_ips"][0]["ip"], "172.24.5.3")
        self.assertEqual(host["previous_ips"][0]["date"], shanghai_today())
        self.assertIn("同机改址", host["previous_ips"][0]["note"])
        moved = self.store.get_target(int(self.new["id"]), include_secrets=True)
        self.assertEqual(moved["host_id"], "host-172-24-5-3")
        self.assertEqual(moved["ip"], "192.168.198.64")
        self.assertEqual(moved["ssh_password"], "secret-new")
        self.assertFalse(moved["retired"])
        kept = self.store.get_target(int(self.old["id"]), include_secrets=True)
        self.assertTrue(kept["retired"])
        self.assertEqual(kept["ip"], "172.24.5.3")
        self.assertEqual(kept["host_id"], "host-172-24-5-3")
        self.assertEqual(kept["ssh_password"], "secret-old")
        self.assertEqual(len(self.store.list_targets()), 2)
        self.assertEqual(self.storage.history_count("host-172-24-5-3"), self.history_before)

        # 停用目标不再驱动展示。清掉手工地址后，应落到仍启用的新 IP。
        code, meta = api_mod.handle_host_meta_update(
            self.storage,
            "host-172-24-5-3",
            b'{"address":""}',
        )
        self.assertEqual(code, 200, meta)
        cleared = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(cleared["address"], "192.168.198.64")
        self.assertEqual(cleared["previous_ips"][0]["ip"], "172.24.5.3")
        self.assertEqual(cleared["owner"], "")

        code, owner = api_mod.handle_host_meta_update(
            self.storage,
            "host-172-24-5-3",
            json.dumps({"owner": "甲"}).encode("utf-8"),
        )
        self.assertEqual(code, 200, owner)
        saved = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(saved["owner"], "甲")
        self.assertEqual(saved["previous_ips"][0]["ip"], "172.24.5.3")

    def test_duplicate_new_ip_refuses_without_writes(self) -> None:
        extra = self.store.add_target(
            {"ip": "192.168.198.64", "host_id": "other-box", "ssh_password": "x"}
        )
        code, resp = self._call(
            {"new_ip": "192.168.198.64", "note": "冲突", "apply": True}
        )
        self.assertEqual(code, 400)
        self.assertIn("多个", resp["error"])
        self.assertEqual(self.storage.get_host("host-172-24-5-3")["address"], "172.24.5.3")
        self.assertFalse(self.store.get_target(int(self.old["id"]))["retired"])
        self.assertEqual(
            self.store.get_target(int(extra["id"]))["host_id"], "other-box"
        )
        self.assertEqual(self.storage.history_count("host-172-24-5-3"), self.history_before)

    def test_host_row_id_unchanged_in_sqlite(self) -> None:
        self._call(
            {"new_ip": "192.168.198.64", "note": "同机", "apply": True}
        )
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT host_id FROM hosts").fetchall()
            hist = conn.execute(
                "SELECT COUNT(*) FROM metrics_history WHERE host_id=?",
                ("host-172-24-5-3",),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual([row[0] for row in rows], ["host-172-24-5-3"])
        self.assertEqual(hist, self.history_before)


if __name__ == "__main__":
    unittest.main()
