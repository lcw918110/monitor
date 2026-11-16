#!/usr/bin/env python3
"""离线导出使用中文表头，并另附字段说明。JSON 仍用英文键。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from urllib.request import urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center.export_labels import field_guide_text
from center.server import make_handler
from center.storage import Storage
from http.server import ThreadingHTTPServer


class ExportChineseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "t.db"), retention_days=7)
        self.now = int(time.time())
        api_mod.handle_metrics_post(
            self.storage,
            json.dumps(
                {
                    "host_id": "h1",
                    "hostname": "one",
                    "host_type": "cpu",
                    "timestamp": self.now - 10,
                    "system": {
                        "cpu_percent": 12.5,
                        "disk_percent": 40,
                        "disk_read_mbps": 8.192,
                        "disk_write_mbps": 1.5,
                        "disk_io_util_percent": 7,
                        "net_rx_mbps": 3,
                        "primary_ip": "10.1.1.8",
                    },
                }
            ).encode("utf-8"),
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hosts_csv_is_chinese_and_json_keeps_english_keys(self) -> None:
        code, text = api_mod.handle_export_csv(self.storage)
        self.assertEqual(code, 200)
        header = text.splitlines()[0]
        self.assertIn("主机标识", header)
        self.assertIn("CPU利用率（%）", header)
        self.assertIn("磁盘占用率（%）", header)
        self.assertIn("磁盘读取速率（Mbps）", header)
        self.assertIn("磁盘写入速率（Mbps）", header)
        self.assertIn("磁盘I/O利用率（%）", header)
        self.assertIn("网络接收速率（Mbps）", header)
        self.assertNotIn("host_id", header)
        self.assertNotIn("cpu_percent", header)
        self.assertIn("h1", text)
        self.assertIn("8.192", text)
        code, payload = api_mod.handle_export_json(self.storage)
        self.assertEqual(code, 200)
        host = payload["hosts"][0]
        self.assertEqual(host["host_id"], "h1")
        self.assertEqual(host["cpu_percent"], 12.5)
        self.assertEqual(host["disk_read_mbps"], 8.192)
        self.assertEqual(host["disk_io_util_percent"], 7)

    def test_period_csv_and_field_guides(self) -> None:
        code, text = api_mod.handle_export_period_csv(self.storage, "minutes=60")
        self.assertEqual(code, 200)
        header = text.splitlines()[0]
        self.assertIn("磁盘读取速率平均（Mbps）", header)
        self.assertIn("磁盘I/O利用率平均（%）", header)
        self.assertIn("磁盘I/O利用率繁忙占比（0～1）", header)
        self.assertNotIn("disk_read_mbps_avg", header)
        self.assertIn("8.19", text)
        guide = field_guide_text()
        self.assertIn("hosts.json", guide)
        self.assertIn("英文", guide)
        self.assertIn("不是 iostat", guide)
        self.assertIn("闲忙", guide)
        code, fields = api_mod.handle_export_hosts_fields_csv()
        self.assertEqual(code, 200)
        self.assertTrue(fields.splitlines()[0].startswith("字段,含义,单位"))
        self.assertIn("磁盘占用率（%）", fields)
        self.assertNotIn("\n含义,", "\n" + text)

    def test_download_keeps_utf8_bom(self) -> None:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.storage, ""))
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            base = "http://127.0.0.1:%s" % httpd.server_address[1]
            for path in (
                "/api/v1/export/hosts.csv",
                "/api/v1/export/period-stats.csv?minutes=60",
                "/api/v1/export/field-guide.txt",
                "/api/v1/export/hosts-fields.csv",
                "/api/v1/export/period-stats-fields.csv",
                "/api/v1/export/rankings-fields.csv",
            ):
                with urlopen(base + path, timeout=5) as resp:
                    body = resp.read()
                self.assertTrue(body.startswith(b"\xef\xbb\xbf"), path)
                self.assertIn("磁盘", body.decode("utf-8-sig"))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
