#!/usr/bin/env python3
"""部署模块基础测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.deploy_api import handle_add_target, handle_import_targets, handle_save_settings
from center.deploy_store import DeployStore


class DeployStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeployStore(os.path.join(self.tmp.name, "d.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settings_and_targets(self) -> None:
        code, resp = handle_save_settings(
            self.store,
            b'{"public_center_url":"http://10.0.0.8:8080","default_ssh_user":"ubuntu"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["settings"]["public_center_url"], "http://10.0.0.8:8080")

        code, resp = handle_add_target(
            self.store,
            b'{"ip":"10.0.0.21","host_id":"npu-21","host_type":"npu"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["target"]["host_id"], "npu-21")
        self.assertEqual(resp["target"]["host_type"], "gpu")  # 旧 npu → gpu

        code, resp = handle_import_targets(
            self.store,
            b'{"text":"10.0.0.22 npu-22\\n10.0.0.23\\n"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["imported"], 2)
        self.assertEqual(len(self.store.list_targets()), 3)


if __name__ == "__main__":
    unittest.main()
