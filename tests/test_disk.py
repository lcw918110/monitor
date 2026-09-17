#!/usr/bin/env python3
"""多挂载点解析、过滤、汇总与 Center 兼容。"""

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

from agent.metrics.disk import (
    collect_disks,
    mount_is_noise,
    parse_df_kp,
    parse_proc_mounts,
    select_local_mounts,
    summarize_disks,
    unescape_proc_field,
)
from center import api as api_mod
from center.anomaly import judge_host_payload
from center.storage import Storage

PROC_MOUNTS = """\
/dev/sda1 / ext4 rw,relatime 0 0
/dev/sda2 /boot ext4 rw,relatime 0 0
/dev/sdb1 /data xfs rw,relatime 0 0
/dev/sda1 /bind\\040copy ext4 rw,bind 0 0
tmpfs /tmp tmpfs rw,nosuid,nodev 0 0
overlay /var/lib/docker/overlay2/abc/merged overlay rw,relatime 0 0
nfs.example.com:/export /mnt/nfs nfs4 rw,relatime 0 0
/dev/loop0 /snap/core/1234 squashfs ro,nodev 0 0
/dev/nvme0n1p1 /boot/efi vfat rw,relatime 0 0
proc /proc proc rw 0 0
udev /dev devtmpfs rw 0 0
cursor-agent-store /cursor/stores fuse.agent-store rw 0 0
"""

DF_KP = """\
Filesystem     1024-blocks      Used Available Capacity Mounted on
/dev/disk3s1s1   488555536  11111111 400000000      3%   /
devfs                  201       201         0    100%   /dev
map auto_home            0         0         0     100%  /System/Volumes/Data/home
/dev/disk3s5     488555536 222222222 400000000     36%   /System/Volumes/Data
"""


def _row(mount, device="/dev/sda", fstype="ext4", total_gb=100, used_gb=40, dev_id=1):
    total_b = int(total_gb * 1024 * 1024 * 1024)
    used_b = int(used_gb * 1024 * 1024 * 1024)
    return {
        "device": device,
        "mount": mount,
        "fstype": fstype,
        "total_bytes": total_b,
        "used_bytes": used_b,
        "dev_id": dev_id,
    }


class ParseFilterTests(unittest.TestCase):
    def test_unescape_space(self) -> None:
        self.assertEqual(unescape_proc_field("/bind\\040copy"), "/bind copy")

    def test_parse_proc_mounts(self) -> None:
        rows = parse_proc_mounts(PROC_MOUNTS)
        mounts = [r["mount"] for r in rows]
        self.assertIn("/", mounts)
        self.assertIn("/data", mounts)
        self.assertIn("/bind copy", mounts)
        data = [r for r in rows if r["mount"] == "/data"][0]
        self.assertEqual(data["fstype"], "xfs")
        self.assertEqual(data["device"], "/dev/sdb1")

    def test_noise_filters(self) -> None:
        self.assertTrue(mount_is_noise("tmpfs", "/tmp", "tmpfs"))
        self.assertTrue(
            mount_is_noise("overlay", "/var/lib/docker/overlay2/abc/merged", "overlay")
        )
        self.assertTrue(mount_is_noise("nfs.example.com:/export", "/mnt/nfs", "nfs4"))
        self.assertTrue(mount_is_noise("/dev/loop0", "/snap/core/1234", "squashfs"))
        self.assertTrue(mount_is_noise("/dev/nvme0n1p1", "/boot/efi", "vfat"))
        self.assertTrue(mount_is_noise("cursor-agent-store", "/cursor/stores", "fuse.agent-store"))
        self.assertFalse(mount_is_noise("/dev/sda1", "/", "ext4"))
        self.assertFalse(mount_is_noise("/dev/sdb1", "/data", "xfs"))
        self.assertFalse(mount_is_noise("/dev/sda2", "/boot", "ext4"))

    def test_parse_df_kp(self) -> None:
        rows = parse_df_kp(DF_KP)
        self.assertEqual(rows[0]["mount"], "/")
        self.assertGreater(rows[0]["total_bytes"], 0)
        mounts = [r["mount"] for r in rows]
        self.assertIn("/dev", mounts)

    def test_select_skips_noise_tiny_and_dedups(self) -> None:
        gb = 1024 * 1024 * 1024
        candidates = [
            _row("/", "/dev/sda1", "ext4", 100, 50, dev_id=10),
            _row("/bind copy", "/dev/sda1", "ext4", 100, 50, dev_id=10),
            _row("/data", "/dev/sdb1", "xfs", 2000, 1800, dev_id=20),
            _row("/boot", "/dev/sda2", "ext4", 0.5, 0.2, dev_id=11),
            _row("/tmp", "tmpfs", "tmpfs", 64, 1, dev_id=99),
            _row(
                "/var/lib/docker/overlay2/x/merged",
                "overlay",
                "overlay",
                100,
                10,
                dev_id=88,
            ),
            _row("/tiny", "/dev/sdc1", "ext4", 0.4, 0.1, dev_id=30),
        ]
        selected = select_local_mounts(candidates, primary_path="/")
        mounts = [d["mount"] for d in selected]
        self.assertEqual(mounts, ["/data", "/"])
        self.assertNotIn("/bind copy", mounts)
        self.assertNotIn("/boot", mounts)
        self.assertNotIn("/tmp", mounts)
        self.assertNotIn("/tiny", mounts)

    def test_primary_kept_even_if_tiny(self) -> None:
        selected = select_local_mounts(
            [_row("/opt/disk", "/dev/sde1", "ext4", 0.2, 0.1, dev_id=7)],
            primary_path="/opt/disk",
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["mount"], "/opt/disk")

    def test_summary_max_percent_sum_capacity(self) -> None:
        selected = select_local_mounts(
            [
                _row("/", "/dev/sda1", "ext4", 100, 20, dev_id=1),
                _row("/data", "/dev/sdb1", "xfs", 400, 360, dev_id=2),
            ],
            primary_path="/",
        )
        summary = summarize_disks(selected)
        self.assertEqual(summary["disk_count"], 2)
        self.assertEqual(summary["disk_percent"], 90.0)
        self.assertEqual(summary["disk_total_gb"], 500.0)
        self.assertEqual(summary["disk_used_gb"], 380.0)
        self.assertEqual(summary["disks"][0]["mount"], "/data")
        self.assertNotIn("total_bytes", summary["disks"][0])
        self.assertNotIn("dev_id", summary["disks"][0])

    def test_collect_disks_live(self) -> None:
        data = collect_disks("/")
        self.assertIn("disks", data)
        self.assertIsInstance(data["disks"], list)
        self.assertGreaterEqual(data["disk_count"], 1)
        self.assertGreater(data["disk_total_gb"], 0)
        mounts = [d["mount"] for d in data["disks"]]
        self.assertTrue(any(m == "/" or m.startswith("/") for m in mounts))
        for d in data["disks"]:
            self.assertGreaterEqual(d["total_gb"], 0)
            self.assertNotEqual(d["fstype"], "tmpfs")


class CenterCompatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"), offline_seconds=90, retention_days=7
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_old_agent_without_disks_still_ingests(self) -> None:
        payload = {
            "host_id": "legacy",
            "hostname": "legacy",
            "host_type": "cpu",
            "timestamp": int(time.time()),
            "system": {
                "cpu_percent": 10,
                "disk_total_gb": 500,
                "disk_used_gb": 100,
                "disk_percent": 20.0,
            },
        }
        code, resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(payload).encode("utf-8")
        )
        self.assertEqual(code, 200)
        self.assertTrue(resp["ok"])
        _, hosts = api_mod.handle_hosts_list(self.storage)
        row = hosts["hosts"][0]
        self.assertEqual(row["disk_percent"], 20.0)
        self.assertIsNone(row.get("disk_count"))
        _, detail = api_mod.handle_host_detail(self.storage, "legacy")
        self.assertNotIn("disks", detail["payload"]["system"])

    def test_new_agent_disks_in_list_and_detail(self) -> None:
        payload = {
            "host_id": "multi",
            "hostname": "multi",
            "host_type": "cpu",
            "timestamp": int(time.time()),
            "system": {
                "cpu_percent": 10,
                "disk_total_gb": 500,
                "disk_used_gb": 380,
                "disk_percent": 90.0,
                "disk_count": 2,
                "disks": [
                    {
                        "mount": "/data",
                        "device": "/dev/sdb1",
                        "fstype": "xfs",
                        "total_gb": 400,
                        "used_gb": 360,
                        "percent": 90.0,
                    },
                    {
                        "mount": "/",
                        "device": "/dev/sda1",
                        "fstype": "ext4",
                        "total_gb": 100,
                        "used_gb": 20,
                        "percent": 20.0,
                    },
                ],
            },
        }
        code, _resp = api_mod.handle_metrics_post(
            self.storage, json.dumps(payload).encode("utf-8")
        )
        self.assertEqual(code, 200)
        _, hosts = api_mod.handle_hosts_list(self.storage)
        row = hosts["hosts"][0]
        self.assertEqual(row["disk_percent"], 90.0)
        self.assertEqual(row["disk_count"], 2)
        self.assertEqual(row["disk_total_gb"], 500)
        _, detail = api_mod.handle_host_detail(self.storage, "multi")
        disks = detail["payload"]["system"]["disks"]
        self.assertEqual(len(disks), 2)
        self.assertEqual(disks[0]["mount"], "/data")
        compact = Storage._compact_history_payload(payload)
        self.assertNotIn("disks", compact["system"])
        self.assertEqual(compact["system"]["disk_percent"], 90.0)

    def test_anomaly_mentions_fullest_mount(self) -> None:
        result = judge_host_payload(
            {
                "system": {
                    "cpu_percent": 10,
                    "disk_percent": 96.0,
                    "disks": [
                        {"mount": "/", "percent": 20.0},
                        {"mount": "/data", "percent": 96.0},
                    ],
                }
            },
            online=True,
        )
        self.assertEqual(result["system"]["status"], "critical")
        disk_findings = [f for f in result["findings"] if f.get("code") == "disk_critical"]
        self.assertEqual(len(disk_findings), 1)
        self.assertIn("/data", disk_findings[0]["message"])

    def test_anomaly_legacy_disk_percent_only(self) -> None:
        result = judge_host_payload(
            {"system": {"cpu_percent": 10, "disk_percent": 88.0}},
            online=True,
        )
        self.assertEqual(result["system"]["status"], "warn")
        msg = [f["message"] for f in result["findings"] if f.get("code") == "disk_warn"][0]
        self.assertIn("磁盘", msg)
        self.assertNotIn("/data", msg)


if __name__ == "__main__":
    unittest.main()
