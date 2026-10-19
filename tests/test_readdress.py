#!/usr/bin/env python3
"""同机更换 IP：部署目标身份冲突、展示地址优先级、历史 IP 匹配、改址接口。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest import mock

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


class ReaddressUndoTests(ReaddressEndpointTests):
    def _apply(self):
        code, applied = self._call(
            {
                "new_ip": "192.168.198.64",
                "note": "同机改址到 192.168.198.64",
                "apply": True,
            }
        )
        self.assertEqual(code, 200, applied)
        return applied

    def _undo(self, body):
        return api_mod.handle_host_readdress_undo(
            self.storage,
            "host-172-24-5-3",
            json.dumps(body).encode("utf-8"),
            deploy_store=self.store,
        )

    def test_apply_records_undo_and_owner_save_keeps_it(self) -> None:
        self._apply()
        host = self.storage.get_host("host-172-24-5-3")
        undo = host["readdress_undo"]
        self.assertEqual(undo["added_previous_ip"], "172.24.5.3")
        ids = {item["id"]: item for item in undo["targets"]}
        self.assertEqual(ids[int(self.new["id"])]["host_id"], "host-192-168-198-64")
        self.assertFalse(ids[int(self.new["id"])]["retired"])
        self.assertEqual(ids[int(self.old["id"])]["host_id"], "host-172-24-5-3")
        self.assertFalse(ids[int(self.old["id"])]["retired"])
        code, owner = api_mod.handle_host_meta_update(
            self.storage,
            "host-172-24-5-3",
            json.dumps({"owner": "甲"}).encode("utf-8"),
        )
        self.assertEqual(code, 200, owner)
        saved = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(saved["owner"], "甲")
        self.assertEqual(saved["previous_ips"][0]["ip"], "172.24.5.3")
        self.assertEqual(saved["readdress_undo"]["added_previous_ip"], "172.24.5.3")

    def test_preview_then_apply_restores_targets_and_clears_address(self) -> None:
        self._apply()
        code, preview = self._undo({"apply": False})
        self.assertEqual(code, 200, preview)
        self.assertFalse(preview["apply"])
        self.assertEqual(preview["remove_previous_ip"], "172.24.5.3")
        self.assertEqual(self.storage.get_host("host-172-24-5-3")["address"], "192.168.198.64")
        self.assertEqual(
            self.store.get_target(int(self.new["id"]))["host_id"], "host-172-24-5-3"
        )

        code, applied = self._undo({"apply": True})
        self.assertEqual(code, 200, applied)
        self.assertTrue(applied["apply"])
        self.assertFalse(applied["history_rewritten"])
        self.assertEqual(applied["history_rows"], self.history_before)
        host = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(host["host_id"], "host-172-24-5-3")
        self.assertEqual(host["asset"]["address"], "")
        self.assertEqual(host["previous_ips"], [])
        self.assertIsNone(host["asset"].get("readdress_undo"))
        moved = self.store.get_target(int(self.new["id"]), include_secrets=True)
        self.assertEqual(moved["host_id"], "host-192-168-198-64")
        self.assertEqual(moved["ssh_password"], "secret-new")
        self.assertFalse(moved["retired"])
        kept = self.store.get_target(int(self.old["id"]), include_secrets=True)
        self.assertEqual(kept["host_id"], "host-172-24-5-3")
        self.assertEqual(kept["ssh_password"], "secret-old")
        self.assertFalse(kept["retired"])
        self.assertEqual(self.storage.history_count("host-172-24-5-3"), self.history_before)
        code, again = self._undo({"apply": True})
        self.assertEqual(code, 400)
        self.assertIn("没有可撤销", again["error"])

    def test_missing_target_refuses_before_writes(self) -> None:
        self._apply()
        self.store.delete_target(int(self.old["id"]))
        code, resp = self._undo({"apply": True})
        self.assertEqual(code, 400, resp)
        self.assertIn("不存在", resp["error"])
        host = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(host["address"], "192.168.198.64")
        self.assertEqual(host["previous_ips"][0]["ip"], "172.24.5.3")
        self.assertIsNotNone(host["readdress_undo"])
        self.assertEqual(
            self.store.get_target(int(self.new["id"]))["host_id"], "host-172-24-5-3"
        )

    def test_no_record_is_400(self) -> None:
        code, resp = self._undo({})
        self.assertEqual(code, 400)
        self.assertIn("没有可撤销", resp["error"])

    def test_http_undo_route(self) -> None:
        from center.server import make_handler
        from http.server import ThreadingHTTPServer

        self._apply()
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.storage, "", deploy_store=self.store))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        port = httpd.server_address[1]
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:%s/api/v1/hosts/host-172-24-5-3/readdress/undo" % port,
                data=b'{"apply": false}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(body["ok"])
            self.assertFalse(body["apply"])
            self.assertEqual(body["remove_previous_ip"], "172.24.5.3")
        finally:
            httpd.shutdown()
            httpd.server_close()


class PreviousIpsMetaTests(ReaddressEndpointTests):
    def test_empty_list_clears_and_invalid_is_rejected(self) -> None:
        self.storage.set_host_readdress(
            "host-172-24-5-3",
            "192.168.198.64",
            [{"ip": "172.24.5.3", "date": "2026-10-19", "note": "同机改址到 192.168.198.64"}],
            undo={
                "added_previous_ip": "172.24.5.3",
                "new_ip": "192.168.198.64",
                "old_ip": "172.24.5.3",
                "note": "同机改址到 192.168.198.64",
                "targets": [],
            },
        )
        code, bad = api_mod.handle_host_meta_update(
            self.storage,
            "host-172-24-5-3",
            json.dumps({"previous_ips": ["不是地址"]}).encode("utf-8"),
        )
        self.assertEqual(code, 400)
        self.assertIn("无效", bad["error"])
        self.assertEqual(
            self.storage.get_host("host-172-24-5-3")["previous_ips"][0]["ip"],
            "172.24.5.3",
        )
        code, kept = api_mod.handle_host_meta_update(
            self.storage,
            "host-172-24-5-3",
            json.dumps({"owner": "乙"}).encode("utf-8"),
        )
        self.assertEqual(code, 200, kept)
        self.assertEqual(
            self.storage.get_host("host-172-24-5-3")["previous_ips"][0]["note"],
            "同机改址到 192.168.198.64",
        )
        code, cleared = api_mod.handle_host_meta_update(
            self.storage,
            "host-172-24-5-3",
            b'{"previous_ips":[]}',
        )
        self.assertEqual(code, 200, cleared)
        host = self.storage.get_host("host-172-24-5-3")
        self.assertEqual(host["previous_ips"], [])
        self.assertEqual(host["owner"], "乙")
        self.assertEqual(host["readdress_undo"]["added_previous_ip"], "172.24.5.3")
        self.assertEqual(host["asset"]["address"], "192.168.198.64")


class RetiredFlagTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeployStore(os.path.join(self.tmp.name, "d.db"))
        self.target = self.store.add_target(
            {
                "ip": "172.24.5.3",
                "host_id": "host-172-24-5-3",
                "ssh_password": "secret-old",
            }
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_retired_toggle_keeps_credentials_and_identity(self) -> None:
        with mock.patch("center.deploy_api.auto_probe_addresses", return_value=[]) as probed:
            code, resp = handle_update_target(
                self.store,
                int(self.target["id"]),
                b'{"retired": true}',
            )
        self.assertEqual(code, 200, resp)
        self.assertTrue(probed.called)
        row = self.store.get_target(int(self.target["id"]), include_secrets=True)
        self.assertTrue(row["retired"])
        self.assertEqual(row["ip"], "172.24.5.3")
        self.assertEqual(row["host_id"], "host-172-24-5-3")
        self.assertEqual(row["ssh_password"], "secret-old")

        with mock.patch("center.deploy_api.auto_probe_addresses", return_value=[]):
            code, resp = handle_update_target(
                self.store,
                int(self.target["id"]),
                b'{"retired": false}',
            )
        self.assertEqual(code, 200, resp)
        row = self.store.get_target(int(self.target["id"]), include_secrets=True)
        self.assertFalse(row["retired"])
        self.assertEqual(row["ssh_password"], "secret-old")
        self.assertEqual(row["ip"], "172.24.5.3")
        self.assertEqual(row["host_id"], "host-172-24-5-3")

    def test_invalid_retired_is_400_before_probe(self) -> None:
        before = self.store.get_target(int(self.target["id"]), include_secrets=True)
        with mock.patch("center.deploy_api.auto_probe_addresses", return_value=[]) as probed:
            code, resp = handle_update_target(
                self.store,
                int(self.target["id"]),
                b'{"retired":"maybe"}',
            )
        self.assertEqual(code, 400)
        self.assertIn("retired", resp["error"])
        self.assertFalse(probed.called)
        after = self.store.get_target(int(self.target["id"]), include_secrets=True)
        self.assertEqual(after["ssh_password"], before["ssh_password"])
        self.assertFalse(after["retired"])
        self.assertEqual(after["ip"], before["ip"])


if __name__ == "__main__":
    unittest.main()
