#!/usr/bin/env python3
"""重点数据守护：标记、路径变化、网段、阈值、白名单、报文不含文件内容。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.data_guard_spool import clear_records, load_records, persist_records, sibling_enqueue
from agent.metrics.data_guard import inventory_tree, parse_socket_table, select_removable_mounts
from center import api as api_mod
from center.data_guard import handle_data_guard_get, handle_data_guard_write
from center.reports import _data_guard_section, render_markdown
from center.storage import PERIOD_METRIC_KEYS, Storage
from common.data_guard import (
    KIND_CIDR,
    KIND_EGRESS,
    KIND_PATH,
    KIND_REMOVABLE,
    evaluate,
    sanitize,
)


SECRET = "SECRET_FILE_BYTES_9f3c"


def _policy(**overrides):
    policy = {
        "enabled": True,
        "paths": ["/data/important"],
        "allowed_cidrs": ["10.0.0.0/8"],
        "allow_destinations": [],
        "allow_processes": [],
        "egress_mbps": 50,
        "egress_sustain_seconds": 60,
        "egress_volume_mb": None,
        "egress_volume_window_seconds": 3600,
        "baseline_multiplier": None,
        "shrink_ratio": 0.5,
        "archive_min_bytes": 100 * 1024 * 1024,
    }
    policy.update(overrides)
    return policy


def _tx(start, mbps, step, count):
    return [{"ts": start + step * i, "mbps": mbps} for i in range(count)]


class PathAndNetworkTests(unittest.TestCase):
    def test_shrink_and_archive_and_failed_read(self) -> None:
        policy = _policy(archive_min_bytes=1000, egress_mbps=None)
        shrunk = evaluate(
            policy,
            {
                "paths": [
                    {
                        "path": "/data/important",
                        "file_count": 2,
                        "total_bytes": 1000,
                        "complete": True,
                        "previous": {
                            "file_count": 10,
                            "total_bytes": 5_000_000,
                            "complete": True,
                            "archives": [],
                        },
                        "new_archives": [],
                    }
                ]
            },
            ts=1000,
        )
        kinds = [item["kind"] for item in shrunk]
        self.assertIn(KIND_PATH, kinds)
        self.assertEqual(shrunk[0]["reason"], "shrink")

        archived = evaluate(
            policy,
            {
                "paths": [
                    {
                        "path": "/data/important",
                        "file_count": 10,
                        "total_bytes": 5_000_000,
                        "complete": True,
                        "previous": {
                            "file_count": 10,
                            "total_bytes": 5_000_000,
                            "complete": True,
                            "archives": [],
                        },
                        "new_archives": [
                            {"name": "dump.tar.gz", "bytes": 4096, "where": "beside"}
                        ],
                    }
                ]
            },
            ts=1000,
        )
        self.assertEqual(archived[0]["reason"], "archive")
        self.assertEqual(archived[0]["archive_name"], "dump.tar.gz")
        self.assertNotIn("content", archived[0])

        failed = evaluate(
            policy,
            {
                "paths": [
                    {
                        "path": "/data/important",
                        "file_count": None,
                        "total_bytes": None,
                        "complete": False,
                        "error": "read_failed",
                        "previous": {
                            "file_count": 10,
                            "total_bytes": 5_000_000,
                            "complete": True,
                        },
                    }
                ]
            },
            ts=1000,
        )
        self.assertEqual(failed, [])

    def test_inventory_counts_files_without_reading_contents(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        try:
            folder = os.path.join(tmp.name, "tree")
            os.makedirs(folder)
            with open(os.path.join(folder, "a.txt"), "w", encoding="utf-8") as handle:
                handle.write(SECRET)
            os.makedirs(os.path.join(folder, "sub"))
            with open(os.path.join(folder, "sub", "b.txt"), "w", encoding="utf-8") as handle:
                handle.write("x")
            found = inventory_tree(folder)
            self.assertTrue(found["complete"])
            self.assertEqual(found["file_count"], 2)
            self.assertGreater(found["total_bytes"], 0)
            blob = json.dumps(found, ensure_ascii=False)
            self.assertNotIn(SECRET, blob)
            missing = inventory_tree(os.path.join(tmp.name, "nope"))
            self.assertEqual(missing["error"], "read_failed")
            self.assertIsNone(missing["file_count"])
        finally:
            tmp.cleanup()

    def test_cidr_allow_and_deny(self) -> None:
        policy = _policy(egress_mbps=None)
        inside = evaluate(
            policy,
            {"connections": [{"ip": "10.1.2.3", "port": 22, "count": 1, "process": "sshd"}]},
            ts=10,
        )
        self.assertEqual(inside, [])
        outside = evaluate(
            policy,
            {"connections": [{"ip": "8.8.8.8", "port": 443, "count": 2, "process": "curl"}]},
            ts=10,
        )
        self.assertEqual(len(outside), 1)
        self.assertEqual(outside[0]["kind"], KIND_CIDR)
        self.assertEqual(outside[0]["connection_count"], 2)
        self.assertEqual(outside[0]["destinations"][0]["ip"], "8.8.8.8")
        unset = evaluate(
            _policy(allowed_cidrs=[], egress_mbps=None),
            {"connections": [{"ip": "8.8.8.8", "port": 443, "count": 1, "process": "curl"}]},
            ts=10,
        )
        self.assertEqual(unset, [])

    def test_threshold_mbps_and_volume(self) -> None:
        mbps = evaluate(
            _policy(),
            {"tx_samples": _tx(1000, 80, 30, 3), "connections": []},
            ts=1060,
        )
        self.assertTrue(any(item["kind"] == KIND_EGRESS and "mbps" in item["reasons"] for item in mbps))
        quiet = evaluate(
            _policy(),
            {"tx_samples": _tx(1000, 1, 30, 3), "connections": []},
            ts=1060,
        )
        self.assertFalse(any(item["kind"] == KIND_EGRESS for item in quiet))
        volume = evaluate(
            _policy(egress_mbps=None, egress_volume_mb=500, egress_volume_window_seconds=3600),
            {"tx_samples": [{"ts": 1000, "mbps": 100}, {"ts": 1080, "mbps": 100}], "connections": []},
            ts=1080,
        )
        self.assertTrue(any(item["kind"] == KIND_EGRESS and "volume" in item["reasons"] for item in volume))

    def test_allowlist_suppresses_known_backup(self) -> None:
        observation = {
            "connections": [
                {"ip": "203.0.113.10", "port": 873, "count": 4, "process": "rsync"}
            ],
            "tx_samples": _tx(1000, 200, 30, 3),
            "mounts": [{"mount": "/media/backup", "device": "/dev/sdb1", "fstype": "vfat"}],
            "allowlisted_processes": ["rsync"],
        }
        noisy = evaluate(_policy(), observation, ts=1060)
        kinds = {item["kind"] for item in noisy}
        self.assertIn(KIND_EGRESS, kinds)
        self.assertIn(KIND_CIDR, kinds)
        self.assertIn(KIND_REMOVABLE, kinds)
        quiet = evaluate(
            _policy(allow_destinations=["203.0.113.10/32"], allow_processes=["rsync"]),
            observation,
            ts=1060,
        )
        self.assertEqual(quiet, [])

    def test_payload_has_no_file_contents(self) -> None:
        dirty = {
            "content": SECRET,
            "file_content": SECRET,
            "paths": [
                {
                    "path": "/data/important",
                    "content": SECRET,
                    "file_count": 10,
                    "total_bytes": 5_000_000,
                    "complete": True,
                    "previous": {
                        "file_count": 10,
                        "total_bytes": 5_000_000,
                        "complete": True,
                        "archives": [],
                    },
                    "new_archives": [
                        {"name": "dump.tar.gz", "bytes": 4096, "content": SECRET, "where": "under"}
                    ],
                }
            ],
            "connections": [{"ip": "8.8.8.8", "port": 443, "count": 1, "process": "curl", "payload": SECRET}],
        }
        cleaned = sanitize(dirty)
        blob = json.dumps(cleaned, ensure_ascii=False)
        self.assertNotIn(SECRET, blob)
        self.assertNotIn("content", blob)
        events = evaluate(_policy(egress_mbps=None, archive_min_bytes=1000), dirty, ts=10)
        event_blob = json.dumps(events, ensure_ascii=False)
        self.assertNotIn(SECRET, event_blob)
        self.assertNotIn('"content"', event_blob)
        self.assertTrue(events)


class MarkingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(os.path.join(self.tmp.name, "t.db"), offline_seconds=90)
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _post(self, host_id: str, observation=None, ts=None) -> dict:
        body = {
            "host_id": host_id,
            "hostname": host_id,
            "host_type": "cpu",
            "timestamp": ts or self.now,
            "system": {"cpu_percent": 3, "disk_percent": 10, "net_tx_mbps": 1},
        }
        if observation is not None:
            body["data_guard"] = {"observation": observation, "content": SECRET}
        code, payload = api_mod.handle_metrics_post(
            self.storage, json.dumps(body).encode("utf-8")
        )
        self.assertEqual(code, 200)
        return payload

    def _save(self, scope: str, scope_id: str, enabled: bool = True, **extra) -> None:
        raw = {"action": "save", "scope": scope, "scope_id": scope_id, "enabled": enabled}
        raw.update(extra)
        code, payload = handle_data_guard_write(
            self.storage, json.dumps(raw).encode("utf-8")
        )
        self.assertEqual(code, 200, payload)

    def test_mark_host_and_group_and_ignore_unmarked(self) -> None:
        leaked = self._post(
            "plain",
            {
                "paths": [],
                "connections": [{"ip": "8.8.8.8", "port": 1, "count": 1, "process": "curl"}],
                "content": SECRET,
            },
        )
        self.assertEqual(leaked["data_guard"], {"enabled": False})
        plain = self.storage.get_host("plain")
        self.assertNotIn("data_guard", plain["payload"])
        self.assertNotIn(SECRET, json.dumps(plain["payload"]))
        self.assertFalse(plain["data_guard"]["marked"])
        self.assertFalse(plain["data_guard"]["attention"])

        self._save("host", "kept", paths=["/data/important"], allowed_cidrs=["10.0.0.0/8"], egress_mbps=None)
        self._post(
            "kept",
            {
                "connections": [{"ip": "8.8.8.8", "port": 443, "count": 1, "process": "curl"}],
                "paths": [],
                "content": SECRET,
            },
        )
        kept = self.storage.get_host("kept")
        self.assertTrue(kept["data_guard"]["marked"])
        self.assertEqual(kept["data_guard"]["source"], "host")
        self.assertTrue(kept["data_guard"]["attention"])
        self.assertEqual(kept["data_guard"]["events"][0]["kind"], KIND_CIDR)
        stored = json.dumps(kept["payload"], ensure_ascii=False)
        self.assertNotIn(SECRET, stored)
        self.assertNotIn("content", stored)

        self._post("member")
        _err, group = self.storage.create_group("资料组")
        self.storage.assign_host_group("member", group["id"])
        self._save("group", str(group["id"]), paths=["/data/share"])
        code, listing = handle_data_guard_get(self.storage)
        self.assertEqual(code, 200)
        member = next(item for item in listing["hosts"] if item["host_id"] == "member")
        self.assertTrue(member["marked"])
        self.assertEqual(member["source"], "group")
        self._save("host", "member", enabled=False, paths=["/data/share"])
        code, listing = handle_data_guard_get(self.storage)
        member = next(item for item in listing["hosts"] if item["host_id"] == "member")
        self.assertFalse(member["marked"])
        self.assertEqual(member["source"], "host")

    def test_period_counts_and_report_words(self) -> None:
        self._save("host", "kept", egress_mbps=10, egress_sustain_seconds=60)
        base = self.now - 120
        for offset in (0, 60):
            stamp = base + offset
            self._post(
                "kept",
                {
                    "tx_samples": [
                        {"ts": stamp - 60, "mbps": 80},
                        {"ts": stamp, "mbps": 80},
                    ],
                    "connections": [],
                    "paths": [],
                },
                ts=stamp,
            )
        stats = self.storage.host_period_stats("kept", from_ts=base - 10, to_ts=self.now)
        guard = stats["data_guard"]
        self.assertTrue(guard["marked"])
        self.assertGreaterEqual(guard["egress_anomaly_count"], 1)
        self.assertGreaterEqual(guard["egress_anomaly_seconds"], 60)
        self.assertNotIn("egress_anomaly", PERIOD_METRIC_KEYS)
        section = _data_guard_section(
            [
                {
                    "host_id": "kept",
                    "address": "10.0.0.8",
                    "group_name": "资料组",
                    "data_guard": guard,
                }
            ]
        )
        text = render_markdown("使用诊断", [section])
        self.assertIn("重点数据守护", text)
        self.assertIn("外传异常", text)
        self.assertIn("路径完整性变化", text)
        self.assertIn("未允许网段外连", text)
        self.assertIn("可移动磁盘挂载", text)
        self.assertIn("不能保证数据出不去", text)

    def test_export_headers_chinese_and_json_keys_english(self) -> None:
        self._save("host", "kept")
        self._post("kept")
        code, text = api_mod.handle_export_csv(self.storage)
        self.assertEqual(code, 200)
        header = text.splitlines()[0]
        self.assertIn("重点数据机", header)
        self.assertIn("数据外传异常", header)
        self.assertNotIn("data_guard_marked", header)
        code, payload = api_mod.handle_export_json(self.storage)
        host = next(item for item in payload["hosts"] if item["host_id"] == "kept")
        self.assertIn("data_guard", host)
        self.assertTrue(host["data_guard"]["marked"])
        self.assertNotIn("重点数据机", host)
        code, period = api_mod.handle_export_period_csv(self.storage, "minutes=60")
        self.assertEqual(code, 200)
        self.assertIn("外传异常次数", period.splitlines()[0])
        self.assertIn("路径完整性变化时长（秒）", period.splitlines()[0])
        guide = api_mod.handle_export_field_guide_text()[1]
        self.assertIn("不能保证数据出不去", guide)
        self.assertIn("白名单", guide)


class SpoolTests(unittest.TestCase):
    def test_data_guard_failure_enters_metric_spool(self) -> None:
        from agent.spool import Spool, enqueue_data_guard

        tmp = tempfile.TemporaryDirectory()
        try:
            spool = Spool(tmp.name, retention_hours=24, max_samples=10)
            stamp = int(time.time()) - 30
            payload = {
                "host_id": "h",
                "timestamp": stamp,
                "system": {"cpu_percent": 1},
                "data_guard": {"observation": {"paths": [{"path": "/data/important"}]}},
            }
            self.assertEqual(enqueue_data_guard(spool, payload), "queued")
            pending = spool.peek()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["timestamp"], stamp)
            observation = pending[0]["payload"]["data_guard"]["observation"]
            self.assertEqual(observation["paths"][0]["path"], "/data/important")
            self.assertNotIn(SECRET, json.dumps(pending))
        finally:
            tmp.cleanup()

    def test_local_file_and_socket_mount_parsers(self) -> None:
        self.assertFalse(sibling_enqueue([{"ts": 1}]))
        tmp = tempfile.TemporaryDirectory()
        try:
            now = int(time.time())
            persist_records(
                tmp.name,
                [{"ts": now, "observation": {"paths": []}, "events": []}],
                now,
            )
            loaded = load_records(tmp.name, now)
            self.assertEqual(len(loaded), 1)
            clear_records(tmp.name)
            self.assertEqual(load_records(tmp.name, now), [])
        finally:
            tmp.cleanup()
        rows = parse_socket_table(
            "ESTAB 0 0 10.0.0.8:40000 8.8.8.8:443 users:((\"curl\",pid=1,fd=3))\n"
            "ESTAB 0 0 10.0.0.8:40001 127.0.0.1:80\n"
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ip"], "8.8.8.8")
        self.assertEqual(rows[0]["process"], "curl")
        mounts = select_removable_mounts(
            [
                {"device": "/dev/sdb1", "mount": "/media/usb", "fstype": "vfat"},
                {"device": "/dev/sda1", "mount": "/", "fstype": "ext4"},
            ],
            {"sdb"},
        )
        self.assertEqual(len(mounts), 1)
        self.assertEqual(mounts[0]["mount"], "/media/usb")


class StaticCopyTests(unittest.TestCase):
    def test_ui_uses_the_locked_names(self) -> None:
        with open(os.path.join(ROOT, "center/static/app.js"), encoding="utf-8") as handle:
            app = handle.read()
        with open(os.path.join(ROOT, "center/static/settings.html"), encoding="utf-8") as handle:
            settings = handle.read()
        for name in ("重点数据机", "数据外传异常", "外传异常", "路径完整性变化", "未允许网段外连", "可移动磁盘挂载"):
            self.assertIn(name, app)
        self.assertIn("不能保证数据出不去", settings)
        self.assertIn("白名单", settings)


if __name__ == "__main__":
    unittest.main()
