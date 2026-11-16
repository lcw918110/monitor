#!/usr/bin/env python3
"""本地暂存、按采集时间入库，以及两种历史中断。"""

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

from agent.spool import (
    Spool,
    apply_flush_result,
    build_batch,
    resolve_spool_dir,
    should_spool,
)
from center import api as api_mod
from center import reports as reports_mod
from center.export_labels import field_guide_text
from center.storage import BACKFILL_LABEL, NODATA_LABEL, Storage, classify_offline_history


def _sample(host_id, ts, cpu=10):
    return {
        "host_id": host_id,
        "hostname": host_id,
        "host_type": "cpu",
        "timestamp": ts,
        "system": {"cpu_percent": cpu, "mem_percent": 20, "disk_percent": 30},
    }


class SpoolQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1_700_000_000
        self.spool = Spool(
            os.path.join(self.tmp.name, "data", "spool"),
            retention_hours=24,
            batch_size=2,
            clock=lambda: self.now,
        )

    def test_enqueue_flush_and_restart_keep_original_timestamp(self):
        payload = _sample("box", self.now - 30, cpu=41)
        payload["token"] = "secret"
        self.spool.enqueue(payload)
        again = Spool(self.spool.directory, retention_hours=24, batch_size=2, clock=lambda: self.now)
        self.assertEqual(again.pending_count(), 1)
        stored = again.peek_batch()[0]
        self.assertEqual(stored["timestamp"], self.now - 30)
        self.assertNotIn("token", stored)
        self.assertNotIn("backfill", stored)

        body = build_batch(again, token="secret")
        self.assertEqual(len(body["samples"]), 1)
        self.assertTrue(body["samples"][0]["backfill"])
        self.assertEqual(body["samples"][0]["timestamp"], self.now - 30)
        self.assertEqual(body["samples"][0]["token"], "secret")
        self.assertTrue(apply_flush_result(again, 1, 0, 200, {"spool_retention_hours": 24}))
        self.assertEqual(again.pending_count(), 0)

    def test_one_batch_leaves_the_rest(self):
        for offset in (40, 30, 20):
            self.spool.enqueue(_sample("box", self.now - offset))
        body = build_batch(self.spool, token="")
        self.assertEqual(len(body["samples"]), 2)
        self.assertEqual(body["samples"][0]["timestamp"], self.now - 40)
        apply_flush_result(self.spool, 2, 0, 200, {})
        self.assertEqual(self.spool.pending_count(), 1)
        self.assertEqual(self.spool.peek_batch()[0]["timestamp"], self.now - 20)
        self.assertFalse(apply_flush_result(self.spool, 1, 0, 0, {}))
        self.assertEqual(self.spool.pending_count(), 1)

    def test_overflow_drops_oldest_and_records_event(self):
        stale = _sample("box", self.now - 25 * 3600, cpu=1)
        fresh = _sample("box", self.now - 15, cpu=2)
        overflow = self.spool.enqueue(stale)
        self.assertIsNotNone(overflow)
        self.assertEqual(overflow["dropped"], 1)
        self.assertEqual(overflow["from_ts"], stale["timestamp"])
        self.assertEqual(overflow["to_ts"], stale["timestamp"])
        self.assertEqual(self.spool.pending_count(), 0)
        self.spool.enqueue(fresh)
        self.now += 25 * 3600
        newer = _sample("box", self.now, cpu=3)
        dropped = self.spool.enqueue(newer)
        self.assertEqual(dropped["dropped"], 1)
        self.assertEqual(dropped["from_ts"], fresh["timestamp"])
        self.assertEqual(self.spool.pending_count(), 1)
        self.assertEqual(self.spool.peek_batch()[0]["timestamp"], newer["timestamp"])
        events = self.spool.pending_overflows()
        self.assertEqual(len(events), 2)
        self.assertEqual(sum(item["dropped"] for item in events), 2)

    def test_center_hint_overrides_local_retention(self):
        local = Spool(os.path.join(self.tmp.name, "hint"), retention_hours=48, clock=lambda: self.now)
        local.enqueue(_sample("box", self.now - 30 * 3600))
        self.assertEqual(local.pending_count(), 1)
        self.assertTrue(local.note_retention_hint(24))
        self.assertEqual(local.effective_retention_hours(), 24)
        self.assertEqual(local.pending_count(), 0)
        self.assertEqual(local.pending_overflows()[0]["dropped"], 1)
        restarted = Spool(local.directory, retention_hours=48, clock=lambda: self.now)
        self.assertEqual(restarted.effective_retention_hours(), 24)

    def test_install_directory_resolution_and_retry_policy(self):
        self.assertEqual(resolve_spool_dir({}), os.path.join("data", "spool"))
        self.assertEqual(
            resolve_spool_dir({"data_dir": "/opt/monitor-agent/data"}),
            "/opt/monitor-agent/data/spool",
        )
        self.assertEqual(resolve_spool_dir({"spool_dir": "/tmp/queue"}), "/tmp/queue")
        self.assertTrue(should_spool(0))
        self.assertTrue(should_spool(503))
        self.assertTrue(should_spool(429))
        self.assertFalse(should_spool(400))
        self.assertFalse(should_spool(401))


class IngestAndClassifyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=7,
            spool_retention_hours=24,
        )
        self.now = int(time.time())

    def test_batch_uses_collection_timestamp_and_dedups(self):
        live_ts = self.now - 5
        old_ts = self.now - 1000
        code, live = api_mod.handle_metrics_post(
            self.storage, json.dumps(_sample("box", live_ts, cpu=11)).encode("utf-8")
        )
        self.assertEqual(code, 200)
        self.assertFalse(live["duplicate"])
        self.assertEqual(live["spool_retention_hours"], 24)
        batch = {
            "samples": [
                dict(_sample("box", old_ts, cpu=22), backfill=True),
                dict(_sample("box", old_ts, cpu=99), backfill=True),
            ],
            "overflows": [
                {"host_id": "box", "from_ts": old_ts - 50, "to_ts": old_ts - 40, "dropped": 3}
            ],
        }
        code, resp = api_mod.handle_metrics_batch(
            self.storage, json.dumps(batch).encode("utf-8")
        )
        self.assertEqual(code, 200, resp)
        self.assertEqual(resp["accepted"], 1)
        self.assertEqual(resp["duplicate"], 1)
        self.assertEqual(resp["overflows"], 1)
        code, again = api_mod.handle_metrics_batch(
            self.storage, json.dumps(batch).encode("utf-8")
        )
        self.assertEqual(code, 200, again)
        self.assertEqual(again["accepted"], 0)
        self.assertEqual(again["duplicate"], 2)
        self.assertEqual(again["overflows"], 0)

        host = self.storage.get_host("box")
        self.assertEqual(host["last_seen"], live_ts)
        self.assertTrue(host["online"])
        points = self.storage.host_history("box", from_ts=old_ts - 10, to_ts=self.now, limit=20)
        by_ts = {point["ts"]: point for point in points}
        self.assertEqual(set(by_ts), {old_ts, live_ts})
        self.assertTrue(by_ts[old_ts]["backfill"])
        self.assertFalse(by_ts[live_ts]["backfill"])
        self.assertEqual(by_ts[old_ts]["cpu_percent"], 22)
        self.assertEqual(self.storage.history_count("box"), 2)

    def test_backfill_does_not_land_on_receive_time(self):
        collected = 1_600_000_000
        before = int(time.time())
        code, _resp = api_mod.handle_metrics_batch(
            self.storage,
            json.dumps(
                {"samples": [dict(_sample("late", collected, cpu=7), backfill=True)]}
            ).encode("utf-8"),
        )
        self.assertEqual(code, 200)
        after = int(time.time())
        conn = sqlite3.connect(self.storage.db_path)
        try:
            row = conn.execute(
                "SELECT ts, backfill, received_at FROM metrics_history WHERE host_id='late'"
            ).fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], collected)
        self.assertEqual(row[1], 1)
        self.assertGreaterEqual(row[2], before)
        self.assertLessEqual(row[2], after)
        self.assertLess(collected, before)
        host = self.storage.get_host("late")
        self.assertEqual(host["last_seen"], collected)
        self.assertFalse(host["online"])

    def test_historical_split_and_period_overview(self):
        # 只看实时点会从 91 断到 500；补报把 200 之后填上，91～200 仍然没有样本。
        split = classify_offline_history(
            [{"ts": 0, "backfill": False}, {"ts": 200, "backfill": True}, {"ts": 210, "backfill": False}],
            0,
            300,
            90,
            first_ts=0,
            overflows=[{"from_ts": 100, "to_ts": 180, "dropped": 4}],
        )
        nodata = split["nodata"]["intervals"]
        caught = split["backfill"]["intervals"]
        self.assertEqual(nodata[0]["label"], NODATA_LABEL)
        self.assertEqual(nodata[0]["from_ts"], 91)
        self.assertEqual(nodata[0]["to_ts"], 200)
        self.assertEqual(nodata[0]["seconds"], 109)
        self.assertTrue(nodata[0]["overflow"])
        self.assertIn("不会再补上", nodata[0]["note"])
        self.assertEqual(caught[0]["label"], BACKFILL_LABEL)
        self.assertEqual(caught[0]["from_ts"], 200)
        self.assertEqual(caught[0]["to_ts"], 210)
        self.assertEqual(caught[0]["seconds"], 10)
        self.assertEqual(split["overflow_dropped"], 4)

        # 全程补上：实时点只有窗口两头，中间不再是「离线（无数据）」。
        filled = [{"ts": 0, "backfill": False}]
        filled.extend({"ts": ts, "backfill": True} for ts in range(15, 500, 15))
        filled.append({"ts": 500, "backfill": False})
        caught_up = classify_offline_history(filled, 0, 500, 90, first_ts=0)
        self.assertEqual(caught_up["nodata"]["interval_count"], 0)
        self.assertEqual(caught_up["backfill"]["intervals"][0]["from_ts"], 91)
        self.assertEqual(caught_up["backfill"]["intervals"][0]["to_ts"], 500)
        self.assertEqual(caught_up["backfill"]["intervals"][0]["seconds"], 409)

        start = self.now - 500
        self.storage.upsert_metric(_sample("gappy", start, cpu=10), backfill=False)
        self.storage.upsert_metric(_sample("gappy", start + 200, cpu=30), backfill=True)
        self.storage.upsert_metric(_sample("gappy", start + 210, cpu=40), backfill=False)
        self.storage.record_overflow("gappy", start + 100, start + 180, 4)
        code, stats = api_mod.handle_cluster_period_stats(
            self.storage, "from_ts=%s&to_ts=%s" % (start, start + 300)
        )
        self.assertEqual(code, 200, stats)
        offline = stats["offline"]
        self.assertEqual(offline["nodata"]["label"], "离线（无数据）")
        self.assertEqual(offline["backfill"]["label"], "断网（已补报）")
        self.assertGreaterEqual(offline["nodata"]["interval_count"], 1)
        self.assertGreaterEqual(offline["backfill"]["interval_count"], 1)
        self.assertGreater(offline["overflow_dropped"], 0)
        host = next(item for item in stats["hosts"] if item["host_id"] == "gappy")
        self.assertEqual(host["metrics"]["cpu_percent"]["sample_count"], 3)
        self.assertEqual(host["offline"]["nodata"]["label"], "离线（无数据）")
        self.assertEqual(host["offline"]["backfill"]["label"], "断网（已补报）")
        code, text = api_mod.handle_export_period_csv(
            self.storage, "from_ts=%s&to_ts=%s" % (start, start + 300)
        )
        self.assertEqual(code, 200)
        header = text.splitlines()[0]
        self.assertIn("离线（无数据）段数", header)
        self.assertIn("断网（已补报）累计（秒）", header)
        self.assertNotIn("nodata_intervals", header)

    def test_report_uses_the_two_terms(self):
        sentence = reports_mod._history_gap_sentence(
            {
                "nodata": {"interval_count": 2, "total_seconds": 120},
                "backfill": {"interval_count": 1, "total_seconds": 400},
                "overflow_dropped": 3,
            }
        )
        self.assertIn("「离线（无数据）」2 段", sentence)
        self.assertIn("「断网（已补报）」1 段", sentence)
        self.assertIn("丢弃 3 条", sentence)
        self.assertNotIn("网络中断", sentence)
        code, payload = reports_mod.handle_generate_report(
            self.storage,
            json.dumps({"period": "day", "as_of": self.now}).encode("utf-8"),
        )
        self.assertEqual(code, 200, payload)
        markdown = payload["report"]["markdown"]
        self.assertIn("离线（无数据）", markdown)
        self.assertIn("断网（已补报）", markdown)
        self.assertIn("闲忙仍只看 CPU 和加速卡", markdown)
        guide = field_guide_text()
        self.assertIn("离线（无数据）", guide)
        self.assertIn("断网（已补报）", guide)
        self.assertIn("24", guide)
        with open(os.path.join(ROOT, "center/static/app.js"), encoding="utf-8") as handle:
            page = handle.read()
        self.assertIn("离线（无数据）", page)
        self.assertIn("断网（已补报）", page)
        self.assertIn("当前只记为离线", page)


class InstallPathTests(unittest.TestCase):
    def test_upgrade_sync_keeps_agent_data_directory(self):
        with open(os.path.join(ROOT, "scripts/lib/sync_tree.sh"), encoding="utf-8") as handle:
            sync = handle.read()
        self.assertIn("--exclude 'data/'", sync)


if __name__ == "__main__":
    unittest.main()
