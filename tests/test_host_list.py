#!/usr/bin/env python3
"""主机完整地址解析与资源分组。"""

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
from center.deploy_store import DeployStore
from center.hostaddr import (
    is_partial_ipv4,
    is_short_hostname,
    normalize_ip,
    resolve_host_address,
)
from center.storage import Storage


class HostAddrTests(unittest.TestCase):
    def test_normalize_and_partial(self) -> None:
        self.assertEqual(normalize_ip("192.168.15.95"), "192.168.15.95")
        self.assertEqual(normalize_ip("::ffff:10.0.0.8"), "10.0.0.8")
        self.assertEqual(normalize_ip("127.0.0.1"), "")
        self.assertEqual(normalize_ip("192.168.15"), "")
        self.assertTrue(is_partial_ipv4("192.168.15"))
        self.assertTrue(is_short_hostname("gpu-01"))
        self.assertFalse(is_short_hostname("gpu-01.lab.local"))
        self.assertFalse(is_short_hostname("192.168.15.95"))

    def test_prefer_payload_then_deploy_then_remote(self) -> None:
        self.assertEqual(
            resolve_host_address(
                host_id="gpu-01",
                hostname="gpu-01",
                payload={"system": {"primary_ip": "192.168.15.95"}},
                remote_ip="10.0.0.1",
                deploy_ip="10.0.0.2",
            ),
            "192.168.15.95",
        )
        self.assertEqual(
            resolve_host_address(
                host_id="gpu-01",
                hostname="gpu-01",
                deploy_map={"gpu-01": "192.168.1.10"},
                remote_ip="10.0.0.1",
            ),
            "192.168.1.10",
        )
        self.assertEqual(
            resolve_host_address(
                host_id="node",
                hostname="node",
                remote_ip="10.0.0.8",
            ),
            "10.0.0.8",
        )
        self.assertEqual(
            resolve_host_address(
                host_id="192.168.15",
                hostname="gpu-01",
            ),
            "",
        )


class HostListAddressGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "t.db")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=7)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _post(self, host_id: str, hostname: str = "", system=None, remote_ip: str = "") -> None:
        payload = {
            "host_id": host_id,
            "hostname": hostname or host_id,
            "host_type": "cpu",
            "timestamp": int(time.time()),
            "system": system
            or {
                "cpu_percent": 12.0,
                "mem_percent": 40.0,
                "disk_percent": 20.0,
                "load1": 0.5,
            },
        }
        code, resp = api_mod.handle_metrics_post(
            self.storage,
            json.dumps(payload).encode("utf-8"),
            remote_ip=remote_ip,
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])

    def test_address_from_remote_ip_not_short_hostname(self) -> None:
        self._post("gpu-01", hostname="gpu-01", remote_ip="192.168.15.95")
        _, data = api_mod.handle_hosts_list(self.storage)
        host = data["hosts"][0]
        self.assertEqual(host["hostname"], "gpu-01")
        self.assertEqual(host["address"], "192.168.15.95")

    def test_loopback_remote_does_not_override_payload_ip(self) -> None:
        self._post(
            "local",
            hostname="local",
            system={"cpu_percent": 1, "primary_ip": "192.168.15.10"},
            remote_ip="127.0.0.1",
        )
        _, data = api_mod.handle_hosts_list(self.storage)
        self.assertEqual(data["hosts"][0]["address"], "192.168.15.10")

    def test_address_from_deploy_targets(self) -> None:
        deploy = DeployStore(self.db)
        deploy.add_target({"ip": "10.20.30.40", "host_id": "npu-21", "hostname": "npu-21"})
        self._post("npu-21", hostname="npu-21")
        _, data = api_mod.handle_hosts_list(self.storage)
        self.assertEqual(data["hosts"][0]["address"], "10.20.30.40")

    def test_groups_assign_filter_persist(self) -> None:
        self._post("h1", hostname="h1", remote_ip="10.0.0.1")
        self._post("h2", hostname="h2", remote_ip="10.0.0.2")
        code, created = api_mod.handle_group_create(
            self.storage, json.dumps({"name": "实验室 A"}).encode("utf-8")
        )
        self.assertEqual(code, 200)
        gid = created["group"]["id"]
        code, assigned = api_mod.handle_host_assign_group(
            self.storage, "h1", json.dumps({"group_id": gid}).encode("utf-8")
        )
        self.assertEqual(code, 200)
        self.assertEqual(assigned["group_name"], "实验室 A")

        _, listed = api_mod.handle_hosts_list(self.storage)
        by_id = {h["host_id"]: h for h in listed["hosts"]}
        self.assertEqual(by_id["h1"]["group_name"], "实验室 A")
        self.assertIsNone(by_id["h2"]["group_id"])

        # 重启后仍在（新 Storage 实例，同一 SQLite）
        again = Storage(self.db, offline_seconds=90, retention_days=7)
        groups = again.list_groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["name"], "实验室 A")
        self.assertEqual(groups[0]["host_count"], 1)
        hosts = {h["host_id"]: h for h in again.list_hosts()}
        self.assertEqual(hosts["h1"]["group_id"], gid)

        code, _ = api_mod.handle_group_delete(self.storage, gid)
        self.assertEqual(code, 200)
        _, listed = api_mod.handle_hosts_list(self.storage)
        by_id = {h["host_id"]: h for h in listed["hosts"]}
        self.assertIsNone(by_id["h1"]["group_id"])

    def test_duplicate_group_name_rejected(self) -> None:
        api_mod.handle_group_create(
            self.storage, json.dumps({"name": "gpu"}).encode("utf-8")
        )
        code, resp = api_mod.handle_group_create(
            self.storage, json.dumps({"name": "gpu"}).encode("utf-8")
        )
        self.assertEqual(code, 400)
        self.assertIn("已存在", resp["error"])

    def test_export_includes_address_and_group(self) -> None:
        self._post(
            "h1",
            hostname="stub",
            system={"cpu_percent": 3, "primary_ip": "192.168.1.8"},
        )
        code, created = api_mod.handle_group_create(
            self.storage, json.dumps({"name": "机房"}).encode("utf-8")
        )
        api_mod.handle_host_assign_group(
            self.storage,
            "h1",
            json.dumps({"group_id": created["group"]["id"]}).encode("utf-8"),
        )
        code, text = api_mod.handle_export_csv(self.storage)
        self.assertEqual(code, 200)
        self.assertIn("address", text.splitlines()[0])
        self.assertIn("192.168.1.8", text)
        self.assertIn("机房", text)

    def test_old_hosts_table_migrates_last_remote_ip(self) -> None:
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
            """
        )
        conn.execute(
            """
            INSERT INTO hosts (host_id, hostname, host_type, last_seen, last_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "old",
                "old",
                "cpu",
                int(time.time()),
                json.dumps({"host_id": "old", "system": {"cpu_percent": 1}}),
                int(time.time()),
            ),
        )
        conn.commit()
        conn.close()
        storage = Storage(path)
        code, _resp = api_mod.handle_metrics_post(
            storage,
            json.dumps(
                {
                    "host_id": "old",
                    "hostname": "old",
                    "host_type": "cpu",
                    "timestamp": int(time.time()),
                    "system": {"cpu_percent": 2},
                }
            ).encode("utf-8"),
            remote_ip="10.1.2.3",
        )
        self.assertEqual(code, 200)
        _, data = api_mod.handle_hosts_list(storage)
        self.assertEqual(data["hosts"][0]["address"], "10.1.2.3")


if __name__ == "__main__":
    unittest.main()
