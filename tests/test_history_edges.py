#!/usr/bin/env python3
"""_history_edges：窗口边界与 idx_metrics_host_ts 查询计划。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.storage import Storage


def _opt(value):
    if value is None:
        return None
    return int(value)


def _legacy_edges(db_path, host_id, before_ts):
    """原先那条按主机聚合的定义，只用于对照结果。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """
            SELECT MIN(ts) AS first_ts,
                   MAX(CASE WHEN ts < ? THEN ts END) AS prior_ts,
                   MAX(CASE WHEN ts < ? AND COALESCE(backfill, 0) = 0 THEN ts END)
                       AS prior_live
            FROM metrics_history
            WHERE host_id=?
            """,
            (int(before_ts), int(before_ts), host_id),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None, None, None
    return _opt(row["first_ts"]), _opt(row["prior_ts"]), _opt(row["prior_live"])


class HistoryEdgesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(
            os.path.join(self.tmp.name, "edges.db"),
            offline_seconds=90,
            retention_days=40,
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _insert(self, host_id, ts, backfill):
        conn = sqlite3.connect(self.storage.db_path)
        try:
            conn.execute(
                """
                INSERT INTO metrics_history
                    (host_id, ts, payload, received_at, backfill)
                VALUES (?, ?, '{}', ?, ?)
                """,
                (host_id, int(ts), int(ts), backfill),
            )
            conn.commit()
        finally:
            conn.close()

    def test_empty_host(self):
        self._insert("other", 10, 0)
        self.assertEqual(self.storage._history_edges("nobody", 1000), (None, None, None))

    def test_only_points_inside_window(self):
        # 窗口起点上的点属于窗口内，不算窗口前。
        self._insert("inside", 1000, 0)
        self._insert("inside", 1100, 1)
        self._insert("inside", 1200, None)
        self.assertEqual(
            self.storage._history_edges("inside", 1000),
            (1000, None, None),
        )

    def test_prior_live_point(self):
        self._insert("live", 900, 0)
        self._insert("live", 1100, 0)
        self._insert("live", 5000, 0)
        self.assertEqual(self.storage._history_edges("live", 1000), (900, 900, 900))

    def test_prior_backfill_differs_from_prior_live(self):
        self._insert("gap", 800, 0)
        self._insert("gap", 900, 1)
        self._insert("gap", 1100, 0)
        self.assertEqual(self.storage._history_edges("gap", 1000), (800, 900, 800))

    def test_prior_backfill_without_earlier_live(self):
        self._insert("late", 900, 1)
        self._insert("late", 1100, 0)
        self.assertEqual(self.storage._history_edges("late", 1000), (900, 900, None))

    def test_first_ts_is_earliest_row_not_window_min(self):
        self._insert("early", 100, 1)
        self._insert("early", 900, 0)
        self._insert("early", 1200, 0)
        self._insert("neighbor", 1, 0)
        self.assertEqual(
            self.storage._history_edges("early", 1000),
            (100, 900, 900),
        )

    def test_null_backfill_counts_as_live(self):
        self._insert("nullbf", 700, 1)
        self._insert("nullbf", 900, None)
        self._insert("nullbf", 1100, 0)
        self.assertEqual(
            self.storage._history_edges("nullbf", 1000),
            (700, 900, 900),
        )

    def test_matches_legacy_aggregate(self):
        samples = [
            ("h", 10, 0),
            ("h", 20, 1),
            ("h", 30, None),
            ("h", 40, 0),
            ("h", 50, 1),
            ("other", 15, 0),
            ("other", 35, 1),
            ("other", 45, None),
        ]
        for host_id, ts, backfill in samples:
            self._insert(host_id, ts, backfill)
        for host_id in ("h", "other", "missing"):
            for before in (0, 10, 20, 25, 30, 40, 50, 60):
                with self.subTest(host_id=host_id, before=before):
                    self.assertEqual(
                        self.storage._history_edges(host_id, before),
                        _legacy_edges(self.storage.db_path, host_id, before),
                    )

    def test_query_plan_uses_host_ts_index(self):
        self._insert("plan", 100, 0)
        self._insert("plan", 200, 1)
        self._insert("plan", 300, 0)
        seen = []
        original = self.storage._connect

        class _RecordingConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                text = sql if isinstance(sql, str) else ""
                if text.lstrip().upper().startswith("SELECT") and "metrics_history" in text:
                    seen.append((text, params))
                return self._conn.execute(sql, params)

            def close(self):
                return self._conn.close()

        def connect():
            return _RecordingConn(original())

        self.storage._connect = connect
        try:
            self.assertEqual(self.storage._history_edges("plan", 250), (100, 200, 100))
        finally:
            self.storage._connect = original

        self.assertEqual(len(seen), 3)
        conn = sqlite3.connect(self.storage.db_path)
        try:
            bounded = 0
            for sql, params in seen:
                detail = " | ".join(
                    row[3] for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params)
                )
                self.assertIn("idx_metrics_host_ts", detail)
                self.assertIn("USING", detail)
                self.assertIn("INDEX", detail)
                if "ts <" in sql:
                    bounded += 1
                    self.assertIn("ts<?", detail)
            self.assertEqual(bounded, 2)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
