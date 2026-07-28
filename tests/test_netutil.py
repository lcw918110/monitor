#!/usr/bin/env python3
"""中心对外地址推断测试。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.deploy_store import DeployStore
from center.netutil import ensure_public_center_url, is_loopback_url


class PublicUrlTests(unittest.TestCase):
    def test_loopback_detect(self) -> None:
        self.assertTrue(is_loopback_url("http://127.0.0.1:8080"))
        self.assertTrue(is_loopback_url("http://localhost:8080"))
        self.assertFalse(is_loopback_url("http://192.168.1.8:8080"))

    def test_ensure_replaces_empty_or_loopback(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = DeployStore(os.path.join(tmp.name, "d.db"))
        url = ensure_public_center_url(store, 8080)
        # 在无网环境下可能推断失败，允许空；有网则不应是 loopback
        if url:
            self.assertFalse(is_loopback_url(url))
            self.assertIn(":8080", url)
        store.update_settings({"public_center_url": "http://127.0.0.1:8080"})
        url2 = ensure_public_center_url(store, 8080)
        if url2:
            self.assertFalse(is_loopback_url(url2))
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
