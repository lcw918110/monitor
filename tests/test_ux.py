"""总览体验与运维卫生：白话健康、今日关注、版本串、备份排序。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.main import build_payload
from agent import __version__ as agent_version
from center.anomaly import judge_host_payload
from center.status_text import accel_health_label


class HealthLabelTests(unittest.TestCase):
    def test_notes_outrank_raw_health(self) -> None:
        self.assertEqual(accel_health_label("abnormal", "驱动/管理工具缺失"), "驱动缺失")
        self.assertEqual(accel_health_label("abnormal", "nvidia-smi 不在 PATH"), "工具不在 PATH")
        self.assertEqual(accel_health_label("abnormal", "设备无响应 (rev ff)"), "硬件异常")
        self.assertEqual(accel_health_label("Good", ""), "正常")
        self.assertEqual(accel_health_label("", ""), "未上报")
        self.assertEqual(accel_health_label("abnormal", ""), "异常")

    def test_finding_keeps_raw_health_value(self) -> None:
        judged = judge_host_payload(
            {
                "system": {"cpu_percent": 10, "mem_percent": 10, "disk_percent": 10},
                "accelerators": [
                    {
                        "index": 0,
                        "vendor_label": "寒武纪",
                        "health": "abnormal",
                        "note": "驱动/管理工具缺失",
                        "util_percent": None,
                    }
                ],
            },
            online=True,
        )
        finding = next(item for item in judged["findings"] if item["code"] == "accel_health")
        self.assertIn("驱动缺失", finding["message"])
        self.assertNotIn("Health=", finding["message"])
        self.assertEqual(finding["value"], "abnormal")


class AgentVersionTests(unittest.TestCase):
    def test_payload_tracks_package_version(self) -> None:
        payload = build_payload({"host_id": "h", "host_type": "cpu"})
        self.assertEqual(payload["agent_version"], agent_version)
        self.assertEqual(agent_version, "1.10.0")
        self.assertNotEqual(payload["agent_version"], "1.4.0")


class UxScriptTests(unittest.TestCase):
    def test_node_labels_attention_and_hash(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node 不在 PATH")
        script = r"""
const ux = require("./center/static/ux.js");
const assert = require("assert");
assert.strictEqual(ux.accelHealthLabel("abnormal", "驱动/管理工具缺失"), "驱动缺失");
assert.strictEqual(ux.accelHealthLabel("abnormal", "npu-smi 不在 PATH"), "工具不在 PATH");
assert.strictEqual(ux.accelHealthLabel("abnormal", "设备无响应 (rev ff)"), "硬件异常");
assert.strictEqual(ux.accelHealthLabel("Good", ""), "正常");
const offline = ux.attentionTags({ online: false, disk_percent: 99, anomaly: { findings: [] } }, {});
assert.deepStrictEqual(offline, ["offline"]);
const disk = ux.attentionTags({
  online: true,
  disk_percent: 96,
  anomaly: { findings: [{ code: "disk_critical" }] },
}, { disk_critical_percent: 95 });
assert.ok(disk.indexOf("disk") >= 0);
assert.ok(disk.indexOf("offline") < 0);
const card = ux.attentionTags({
  online: true,
  disk_percent: 10,
  cpu_percent: 10,
  anomaly: { findings: [{ code: "accel_health" }] },
}, {});
assert.deepStrictEqual(card, ["accel"]);
const load = ux.attentionTags({
  online: true,
  cpu_percent: 96,
  load1: 1,
  cpu_count: 8,
  anomaly: { findings: [{ code: "cpu_util_critical" }] },
}, { cpu_critical_percent: 95 });
assert.deepStrictEqual(load, ["load"]);
assert.strictEqual(ux.parseLocationHash("#reports").tab, "reports");
assert.strictEqual(ux.parseLocationHash("#hosts").focus, "hosts");
assert.strictEqual(ux.parseLocationHash("#host=gpu%2F1").hostId, "gpu/1");
assert.strictEqual(ux.parseLocationHash("#host=gpu%2F1").tab, "realtime");
"""
        proc = subprocess.run(
            [node, "-e", script],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)


class BackupSortTests(unittest.TestCase):
    def test_dashed_name_sorts_after_earlier_compact_stamp(self) -> None:
        tmp = tempfile.mkdtemp(prefix="backup-sort-")
        names = [
            "monitor-backup-20200101-020202",
            "monitor-backup-20200101010101",
            "monitor-backup-20200101030303",
            "monitor-backup-100",
        ]
        try:
            paths = []
            for name in names:
                path = os.path.join(tmp, name)
                os.makedirs(path)
                paths.append(path)
            script = """
set -euo pipefail
. "%s/scripts/lib/backup_prune.sh"
backup_paths_oldest_first monitor %s
""" % (
                ROOT,
                " ".join('"%s"' % path for path in paths),
            )
            proc = subprocess.run(
                ["bash", "-c", script],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            ordered = [os.path.basename(line) for line in proc.stdout.splitlines() if line.strip()]
            self.assertEqual(
                ordered,
                [
                    "monitor-backup-100",
                    "monitor-backup-20200101010101",
                    "monitor-backup-20200101-020202",
                    "monitor-backup-20200101030303",
                ],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
