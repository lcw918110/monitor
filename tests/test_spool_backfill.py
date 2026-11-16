#!/usr/bin/env python3
"""本地队列、按采集时间入库，以及两种离线的事后划分。"""

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

from agent.main import submit_payload
from agent.spool import Spool, build_flush_body
from center import api as api_mod
from center.export_labels import field_guide_text
from center.reports import build_report
from center.storage import Storage, split_offline_intervals
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=SHANGHAI).timestamp())


def _sample(host_id: str, ts: int, cpu: float, backfill: bool = False) -> dict:
    body = {
        "host_id": host_id,
        "hostname": host_id,
        "host_type": "cpu",
        "timestamp": ts,
        "system": {
            "cpu_percent": cpu,
            "mem_percent": 10,
            "disk_percent": 10,
            "cpu_count": 4,
        },
    }
    if backfill:
        body["backfill"] = True
    return body


class SpoolTests(unittest.TestCase):
    def test_enqueue_survives_restart_and_flush_keeps_original_timestamp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = os.path.join(tmp.name, "data", "spool")
        spool = Spool(directory, retention_hours=24, batch_size=80)
        collected = int(time.time()) - 120
        payload = _sample("gpu-01", collected, 12.5)
        self.assertEqual(spool.enqueue(payload), "queued")
        again = Spool(directory, retention_hours=24, batch_size=80)
        peeked = again.peek()
        self.assertEqual(len(peeked), 1)
        self.assertEqual(peeked[0]["timestamp"], collected)
        self.assertEqual(peeked[0]["payload"]["system"]["cpu_percent"], 12.5)
        self.assertNotIn("backfill", peeked[0]["payload"])

        sent = []

        def send(url, body):
            sent.append((url, body))
            if url.endswith("/batch"):
                return 200, json.dumps(
                    {"ok": True, "stored_overflows": 0, "spool_retention_hours": 24}
                )
            return 200, json.dumps({"ok": True, "spool_retention_hours": 24})

        code, _body, state = submit_payload(
            {
                "center_url": "http://127.0.0.1:9/api/v1/metrics",
                "token": "t",
                "host_id": "gpu-01",
            },
            _sample("gpu-01", collected + 30, 3),
            again,
            send_fn=send,
        )
        self.assertEqual(code, 200)
        self.assertEqual(state, "sent")
        self.assertEqual(again.peek(), [])
        batch = sent[1][1]
        self.assertTrue(batch["samples"][0]["backfill"])
        self.assertEqual(batch["samples"][0]["timestamp"], collected)

    def test_failed_send_enqueues_and_one_success_flushes_only_one_batch(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        spool = Spool(tmp.name, retention_hours=24, batch_size=2)
        base = int(time.time()) - 80
        calls = {"n": 0}

        def send(url, body):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0, "down"
            if str(url).endswith("/batch"):
                return 200, json.dumps({"ok": True, "stored_overflows": 0})
            return 200, json.dumps({"ok": True, "spool_retention_hours": 24})

        cfg = {"center_url": "http://127.0.0.1:9/api/v1/metrics", "token": ""}
        code, _body, state = submit_payload(cfg, _sample("h", base, 1), spool, send_fn=send)
        self.assertEqual(code, 0)
        self.assertEqual(state, "queued")
        spool.enqueue(_sample("h", base + 10, 1))
        spool.enqueue(_sample("h", base + 20, 1))
        self.assertEqual(len(spool.peek(10)), 3)
        code, _body, state = submit_payload(cfg, _sample("h", base + 30, 1), spool, send_fn=send)
        self.assertEqual(state, "sent")
        left = [item["timestamp"] for item in spool.peek(10)]
        self.assertEqual(left, [base + 20])
        built = build_flush_body(spool, limit=2)
        assert built is not None
        self.assertEqual([item["timestamp"] for item in built[0]["samples"]], [base + 20])

    def test_overflow_drops_oldest_and_records_event(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        now = 2_000_000_000
        spool = Spool(tmp.name, retention_hours=24, max_samples=2)
        self.assertEqual(spool.enqueue(_sample("h", now - 30, 1), now=now), "queued")
        self.assertEqual(spool.enqueue(_sample("h", now - 20, 1), now=now), "queued")
        self.assertEqual(spool.enqueue(_sample("h", now - 10, 1), now=now), "queued")
        left = [item["timestamp"] for item in spool.peek()]
        self.assertEqual(left, [now - 20, now - 10])
        events = spool.pending_overflows()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["reason"], "capacity")
        self.assertEqual(events[0]["dropped"], 1)
        self.assertEqual(events[0]["from_ts"], now - 30)
        restarted = Spool(tmp.name, retention_hours=24, max_samples=2)
        self.assertEqual(
            [item["timestamp"] for item in restarted.peek()],
            [now - 20, now - 10],
        )
        self.assertEqual(restarted.pending_overflows()[0]["dropped"], 1)

        aged = Spool(os.path.join(tmp.name, "aged"), retention_hours=1, max_samples=100)
        old = now - 7200
        self.assertEqual(aged.enqueue(_sample("h", old, 4), now=now), "overflow")
        self.assertEqual(aged.peek(), [])
        self.assertEqual(aged.pending_overflows()[0]["reason"], "retention")
        self.assertEqual(aged.pending_overflows()[0]["from_ts"], old)


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "t.db"),
            offline_seconds=90,
            retention_days=7,
        )
        self.now = int(time.time())

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_history_uses_collection_timestamp_and_dedups(self) -> None:
        collected = self.now - 600
        code, resp = api_mod.handle_metrics_post(
            self.storage,
            json.dumps(_sample("h1", collected, 15)).encode("utf-8"),
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "inserted")
        code, again = api_mod.handle_metrics_post(
            self.storage,
            json.dumps(_sample("h1", collected, 99)).encode("utf-8"),
        )
        self.assertEqual(code, 200)
        self.assertEqual(again["status"], "duplicate")
        points = self.storage.host_history(
            "h1", from_ts=collected - 10, to_ts=self.now, limit=20
        )
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["ts"], collected)
        self.assertGreaterEqual(points[0]["received_at"], self.now - 5)
        self.assertNotEqual(points[0]["received_at"], collected)
        self.assertFalse(points[0]["backfill"])
        self.assertEqual(points[0]["cpu_percent"], 15)

    def test_backfill_does_not_regress_snapshot_and_batch_marks_late_points(self) -> None:
        live_ts = self.now - 5
        api_mod.handle_metrics_post(
            self.storage,
            json.dumps(_sample("h1", live_ts, 11)).encode("utf-8"),
        )
        old_ts = self.now - 500
        code, resp = api_mod.handle_metrics_batch(
            self.storage,
            json.dumps(
                {
                    "host_id": "h1",
                    "samples": [_sample("h1", old_ts, 77, backfill=True)],
                    "overflows": [
                        {
                            "from_ts": old_ts - 100,
                            "to_ts": old_ts - 50,
                            "dropped": 4,
                            "reason": "retention",
                        }
                    ],
                }
            ).encode("utf-8"),
        )
        self.assertEqual(code, 200, resp)
        self.assertEqual(resp["accepted"], 1)
        self.assertEqual(resp["stored_overflows"], 1)
        host = self.storage.get_host("h1")
        assert host is not None
        self.assertEqual(host["last_seen"], live_ts)
        self.assertEqual(host["payload"]["system"]["cpu_percent"], 11)
        self.assertTrue(host["online"])
        points = self.storage.host_history(
            "h1", from_ts=old_ts - 10, to_ts=self.now, limit=20
        )
        by_ts = {point["ts"]: point for point in points}
        self.assertTrue(by_ts[old_ts]["backfill"])
        self.assertFalse(by_ts[live_ts]["backfill"])
        code, again = api_mod.handle_metrics_batch(
            self.storage,
            json.dumps(
                {
                    "host_id": "h1",
                    "samples": [],
                    "overflows": [
                        {
                            "from_ts": old_ts - 100,
                            "to_ts": old_ts - 50,
                            "dropped": 4,
                            "reason": "retention",
                        }
                    ],
                }
            ).encode("utf-8"),
        )
        self.assertEqual(again["stored_overflows"], 0)


class OfflineSplitTests(unittest.TestCase):
    def test_pure_split_labels_gap_and_backfill(self) -> None:
        no_data, filled = split_offline_intervals(
            [(1000, False), (1080, True), (1120, True), (1200, False)],
            1000,
            1250,
            90,
            first_ts=1000,
        )
        self.assertEqual(no_data, [])
        self.assertEqual(filled[0]["label"], "断网（已补报）")
        self.assertEqual(filled[0]["from_ts"], 1091)
        self.assertEqual(filled[0]["to_ts"], 1200)
        self.assertEqual(filled[0]["seconds"], 109)
        dead, late = split_offline_intervals(
            [(1000, False), (2000, False)],
            1000,
            2100,
            90,
            first_ts=1000,
        )
        self.assertEqual(late, [])
        self.assertEqual(dead[0]["label"], "离线（无数据）")
        self.assertEqual(dead[0]["from_ts"], 1091)
        self.assertEqual(dead[0]["to_ts"], 2000)

    def test_period_overview_and_stats_include_backfill(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = Storage(os.path.join(tmp.name, "t.db"), offline_seconds=90, retention_days=7)
        start = int(time.time()) - 3000
        api_mod.handle_metrics_post(
            storage, json.dumps(_sample("alive", start, 10)).encode("utf-8")
        )
        api_mod.handle_metrics_batch(
            storage,
            json.dumps(
                {
                    "host_id": "alive",
                    "samples": [
                        _sample("alive", start + 80, 50, backfill=True),
                        _sample("alive", start + 140, 50, backfill=True),
                    ],
                }
            ).encode("utf-8"),
        )
        api_mod.handle_metrics_post(
            storage, json.dumps(_sample("alive", start + 200, 10)).encode("utf-8")
        )
        api_mod.handle_metrics_post(
            storage, json.dumps(_sample("dead", start, 10)).encode("utf-8")
        )
        api_mod.handle_metrics_post(
            storage, json.dumps(_sample("dead", start + 400, 10)).encode("utf-8")
        )
        code, payload = api_mod.handle_cluster_period_stats(
            storage, "from_ts=%s&to_ts=%s" % (start, start + 250)
        )
        self.assertEqual(code, 200, payload)
        offline = payload["offline"]
        self.assertEqual(offline["no_data"]["label"], "离线（无数据）")
        self.assertEqual(offline["backfill"]["label"], "断网（已补报）")
        self.assertGreater(offline["backfill"]["total_seconds"], 0)
        alive = next(item for item in payload["hosts"] if item["host_id"] == "alive")
        self.assertEqual(alive["metrics"]["cpu_percent"]["avg"], 30)
        self.assertGreater(alive["offline"]["backfill"]["total_seconds"], 0)
        self.assertEqual(alive["offline"]["no_data"]["interval_count"], 0)
        dead = next(item for item in payload["hosts"] if item["host_id"] == "dead")
        self.assertGreater(dead["offline"]["no_data"]["total_seconds"], 90)
        self.assertEqual(dead["offline"]["backfill"]["interval_count"], 0)
        listed = {item["host_id"]: item for item in offline["hosts"]}
        self.assertIn("dead", listed)
        self.assertEqual(
            listed["dead"]["interval_count"],
            dead["offline"]["interval_count"],
        )
        code, text = api_mod.handle_export_period_csv(
            storage, "from_ts=%s&to_ts=%s" % (start, start + 250)
        )
        self.assertEqual(code, 200)
        header = text.splitlines()[0]
        self.assertIn("离线（无数据）段数", header)
        self.assertIn("断网（已补报）累计（秒）", header)
        guide = field_guide_text()
        self.assertIn("离线（无数据）", guide)
        self.assertIn("断网（已补报）", guide)
        self.assertIn("24", guide)

    def test_report_uses_the_two_terms(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        storage = Storage(os.path.join(tmp.name, "t.db"), offline_seconds=90, retention_days=40)
        err, doc = build_report(
            storage,
            "day",
            now=_ts(2026, 10, 5, 0, 10),
            clock=_ts(2026, 10, 5, 0, 10),
            generated_at=_ts(2026, 10, 5, 0, 10),
        )
        self.assertIsNone(err)
        assert doc is not None
        text = doc["markdown"]
        self.assertIn("离线（无数据）", text)
        self.assertIn("断网（已补报）", text)
        self.assertIn("实时页在中断期间只显示「离线」", text)
        self.assertIn("不要把最后一次上报当成仍在运行的利用率", text)


if __name__ == "__main__":
    unittest.main()
