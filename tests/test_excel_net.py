#!/usr/bin/env python3
"""Excel 导入与网络探测测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.deploy_api import auto_probe_addresses, handle_import_excel
from center.deploy_store import DeployStore
from center.xlsx_util import build_import_template, build_xlsx, read_xlsx_rows, rows_to_target_dicts


class ExcelTests(unittest.TestCase):
    def test_roundtrip(self) -> None:
        data = build_import_template()
        rows = read_xlsx_rows(data)
        items = rows_to_target_dicts(rows)
        self.assertGreaterEqual(len(items), 3)
        self.assertEqual(items[0]["ip"], "10.0.0.11")
        self.assertEqual(items[0]["host_type"], "gpu")
        self.assertEqual(items[0]["remote_dir"], "/opt/monitor-agent")
        self.assertEqual(items[2]["host_type"], "cpu")
        self.assertEqual(items[2]["remote_dir"], "/opt/monitor")
        self.assertIn("10.0.0.1", items[0].get("peer_hosts") or "")

    def test_import_api(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = DeployStore(os.path.join(tmp.name, "d.db"))
        code, resp = handle_import_excel(store, build_import_template())
        self.assertEqual(code, 200)
        self.assertEqual(resp["imported"], 3)
        tmp.cleanup()


class ProbeTests(unittest.TestCase):
    def test_auto_probe_on_configured_addresses(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = DeployStore(os.path.join(tmp.name, "d.db"))
        target = store.add_target(
            {
                "ip": "127.0.0.1",
                "host_id": "local",
                "ssh_port": 22,
                "peer_hosts": "本机环回=127.0.0.1",
            }
        )
        results = auto_probe_addresses(store, target=store.get_target(int(target["id"])))
        self.assertGreaterEqual(len(results), 2)
        roles = {r.get("role") for r in results}
        self.assertIn("client", roles)
        self.assertIn("peer", roles)
        refreshed = store.get_target(int(target["id"]))
        self.assertIsNotNone(refreshed)
        self.assertIsNotNone(refreshed.get("net_probed_at"))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
