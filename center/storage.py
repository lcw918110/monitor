"""SQLite 存储：主机快照与历史上报。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


class Storage:
    def __init__(
        self,
        db_path: str,
        offline_seconds: int = 90,
        retention_days: int = 7,
    ) -> None:
        self.db_path = db_path
        self.offline_seconds = offline_seconds
        self.retention_days = retention_days
        self._lock = threading.RLock()
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
        except sqlite3.Error:
            pass
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS hosts (
                        host_id TEXT PRIMARY KEY,
                        hostname TEXT NOT NULL,
                        host_type TEXT NOT NULL,
                        last_seen INTEGER NOT NULL,
                        last_payload TEXT NOT NULL,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS metrics_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        host_id TEXT NOT NULL,
                        ts INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_metrics_host_ts
                        ON metrics_history(host_id, ts);
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def upsert_metric(self, payload: Dict[str, Any]) -> None:
        host_id = payload["host_id"]
        hostname = payload.get("hostname") or host_id
        host_type = payload.get("host_type") or "cpu"
        ts = int(payload.get("timestamp") or time.time())
        raw = json.dumps(payload, ensure_ascii=False)
        compact = json.dumps(self._compact_history_payload(payload), ensure_ascii=False)
        now = int(time.time())

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO hosts (host_id, hostname, host_type, last_seen, last_payload, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(host_id) DO UPDATE SET
                        hostname=excluded.hostname,
                        host_type=excluded.host_type,
                        last_seen=excluded.last_seen,
                        last_payload=excluded.last_payload
                    """,
                    (host_id, hostname, host_type, ts, raw, now),
                )
                conn.execute(
                    "INSERT INTO metrics_history (host_id, ts, payload) VALUES (?, ?, ?)",
                    (host_id, ts, compact),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _compact_history_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        """历史上报只保留趋势所需字段，减小库体积。"""
        system = payload.get("system") or {}
        npus = payload.get("npus") or []
        gpus = payload.get("gpus") or []
        return {
            "host_id": payload.get("host_id"),
            "timestamp": payload.get("timestamp"),
            "system": {
                "cpu_percent": system.get("cpu_percent"),
                "cpu_count": system.get("cpu_count"),
                "mem_percent": system.get("mem_percent"),
                "disk_percent": system.get("disk_percent"),
                "load1": system.get("load1"),
                "load5": system.get("load5"),
                "load15": system.get("load15"),
                "net_rx_mbps": system.get("net_rx_mbps"),
                "net_tx_mbps": system.get("net_tx_mbps"),
            },
            "npus": [
                {
                    "index": n.get("index"),
                    "util_percent": n.get("util_percent"),
                    "temp_c": n.get("temp_c"),
                    "mem_percent": n.get("mem_percent"),
                    "health": n.get("health"),
                }
                for n in npus
            ],
            "gpus": [
                {
                    "index": g.get("index"),
                    "util_percent": g.get("util_percent"),
                    "temp_c": g.get("temp_c"),
                }
                for g in gpus
            ],
        }
    def _is_online(self, last_seen: int, now: Optional[int] = None) -> bool:
        now = now if now is not None else int(time.time())
        return (now - int(last_seen)) <= self.offline_seconds

    @staticmethod
    def _summary_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        system = payload.get("system") or {}
        npus = payload.get("npus") or []
        gpus = payload.get("gpus") or []
        npu_utils = []
        for n in npus:
            if n.get("util_percent") is not None:
                try:
                    npu_utils.append(float(n["util_percent"]))
                except (TypeError, ValueError):
                    pass
        gpu_utils = []
        for g in gpus:
            if g.get("util_percent") is not None:
                try:
                    gpu_utils.append(float(g["util_percent"]))
                except (TypeError, ValueError):
                    pass
        return {
            "cpu_percent": system.get("cpu_percent"),
            "cpu_count": system.get("cpu_count"),
            "mem_percent": system.get("mem_percent"),
            "disk_percent": system.get("disk_percent"),
            "load1": system.get("load1"),
            "npu_count": len(npus),
            "npu_util_avg": round(sum(npu_utils) / len(npu_utils), 2) if npu_utils else None,
            "gpu_count": len(gpus),
            "gpu_util_avg": round(sum(gpu_utils) / len(gpu_utils), 2) if gpu_utils else None,
        }

    def list_hosts(self) -> List[Dict[str, Any]]:
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT host_id, hostname, host_type, last_seen, last_payload FROM hosts ORDER BY host_id"
                ).fetchall()
            finally:
                conn.close()

        result: List[Dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["last_payload"])
            item = {
                "host_id": row["host_id"],
                "hostname": row["hostname"],
                "host_type": row["host_type"],
                "last_seen": row["last_seen"],
                "online": self._is_online(row["last_seen"], now),
            }
            item.update(self._summary_from_payload(payload))
            result.append(item)
        return result

    def get_host(self, host_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT host_id, hostname, host_type, last_seen, last_payload, created_at FROM hosts WHERE host_id=?",
                    (host_id,),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        payload = json.loads(row["last_payload"])
        return {
            "host_id": row["host_id"],
            "hostname": row["hostname"],
            "host_type": row["host_type"],
            "last_seen": row["last_seen"],
            "created_at": row["created_at"],
            "online": self._is_online(row["last_seen"]),
            "payload": payload,
        }

    def cluster_stats(self) -> Dict[str, Any]:
        hosts = self.list_hosts()
        online = [h for h in hosts if h["online"]]
        npu_hosts = [h for h in hosts if h.get("host_type") == "npu" or (h.get("npu_count") or 0) > 0]
        gpu_hosts = [h for h in hosts if h.get("host_type") == "gpu" or (h.get("gpu_count") or 0) > 0]

        npu_cards = sum(int(h.get("npu_count") or 0) for h in hosts)
        gpu_cards = sum(int(h.get("gpu_count") or 0) for h in hosts)
        cpu_cores = sum(int(h.get("cpu_count") or 0) for h in online)

        def _avg(values: List[float]) -> Optional[float]:
            return round(sum(values) / len(values), 2) if values else None

        avg_cpu = _avg([float(h["cpu_percent"]) for h in online if h.get("cpu_percent") is not None])
        avg_mem = _avg([float(h["mem_percent"]) for h in online if h.get("mem_percent") is not None])
        avg_npu = _avg(
            [float(h["npu_util_avg"]) for h in online if h.get("npu_util_avg") is not None]
        )
        avg_gpu = _avg(
            [float(h["gpu_util_avg"]) for h in online if h.get("gpu_util_avg") is not None]
        )

        return {
            "host_total": len(hosts),
            "host_online": len(online),
            "cpu_cores_online": cpu_cores,
            "npu_hosts": len(npu_hosts),
            "npu_cards": npu_cards,
            "gpu_hosts": len(gpu_hosts),
            "gpu_cards": gpu_cards,
            "avg_cpu_percent": avg_cpu,
            "avg_mem_percent": avg_mem,
            "avg_npu_util_percent": avg_npu,
            "avg_gpu_util_percent": avg_gpu,
            "offline_seconds": self.offline_seconds,
            "generated_at": int(time.time()),
        }

    @staticmethod
    def _history_point_from_row(ts: int, payload_text: str) -> Optional[Dict[str, Any]]:
        try:
            payload = json.loads(payload_text)
        except (TypeError, json.JSONDecodeError):
            return None
        system = payload.get("system") or {}
        npus = payload.get("npus") or []
        gpus = payload.get("gpus") or []
        npu_utils: List[float] = []
        npu_temps: List[float] = []
        for n in npus:
            if n.get("util_percent") is not None:
                try:
                    npu_utils.append(float(n["util_percent"]))
                except (TypeError, ValueError):
                    pass
            if n.get("temp_c") is not None:
                try:
                    npu_temps.append(float(n["temp_c"]))
                except (TypeError, ValueError):
                    pass
        gpu_utils: List[float] = []
        gpu_temps: List[float] = []
        for g in gpus:
            if g.get("util_percent") is not None:
                try:
                    gpu_utils.append(float(g["util_percent"]))
                except (TypeError, ValueError):
                    pass
            if g.get("temp_c") is not None:
                try:
                    gpu_temps.append(float(g["temp_c"]))
                except (TypeError, ValueError):
                    pass
        accel_utils = npu_utils + gpu_utils
        accel_temps = npu_temps + gpu_temps
        return {
            "ts": ts,
            "cpu_percent": system.get("cpu_percent"),
            "mem_percent": system.get("mem_percent"),
            "disk_percent": system.get("disk_percent"),
            "load1": system.get("load1"),
            "net_rx_mbps": system.get("net_rx_mbps"),
            "net_tx_mbps": system.get("net_tx_mbps"),
            "npu_util_avg": round(sum(npu_utils) / len(npu_utils), 2) if npu_utils else None,
            "npu_temp_max": max(npu_temps) if npu_temps else None,
            "npu_count": len(npus),
            "gpu_util_avg": round(sum(gpu_utils) / len(gpu_utils), 2) if gpu_utils else None,
            "gpu_temp_max": max(gpu_temps) if gpu_temps else None,
            "gpu_count": len(gpus),
            "accel_util_avg": (
                round(sum(accel_utils) / len(accel_utils), 2) if accel_utils else None
            ),
            "accel_temp_max": max(accel_temps) if accel_temps else None,
        }

    def host_history(
        self,
        host_id: str,
        minutes: int = 60,
        limit: int = 240,
    ) -> List[Dict[str, Any]]:
        """返回主机历史精简序列，供趋势图使用（取时间窗内最近的点）。"""
        minutes = max(1, min(int(minutes), 60 * 24 * 7))
        limit = max(1, min(int(limit), 2000))
        since = int(time.time()) - minutes * 60

        with self._lock:
            conn = self._connect()
            try:
                # 先取最近 limit 条，再按时间正序返回，避免长窗口只拿到最旧点
                rows = conn.execute(
                    """
                    SELECT ts, payload FROM (
                      SELECT ts, payload FROM metrics_history
                      WHERE host_id=? AND ts>=?
                      ORDER BY ts DESC
                      LIMIT ?
                    ) ORDER BY ts ASC
                    """,
                    (host_id, since, limit),
                ).fetchall()
            finally:
                conn.close()

        points: List[Dict[str, Any]] = []
        for row in rows:
            pt = self._history_point_from_row(row["ts"], row["payload"])
            if pt:
                points.append(pt)
        return points

    def host_period_stats(
        self,
        host_id: str,
        minutes: int = 120,
    ) -> Dict[str, Any]:
        """对时间窗内全部历史点做平均/最低/最高统计。"""
        minutes = max(1, min(int(minutes), 60 * 24 * 7))
        since = int(time.time()) - minutes * 60
        # 安全上限：约 7 天 × 10s 采样
        max_rows = 60000

        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT ts, payload FROM metrics_history
                    WHERE host_id=? AND ts>=?
                    ORDER BY ts ASC
                    LIMIT ?
                    """,
                    (host_id, since, max_rows),
                ).fetchall()
            finally:
                conn.close()

        series: Dict[str, List[float]] = {
            "cpu_percent": [],
            "mem_percent": [],
            "disk_percent": [],
            "load1": [],
            "net_rx_mbps": [],
            "net_tx_mbps": [],
            "accel_util_avg": [],
            "accel_temp_max": [],
        }
        first_ts = None
        last_ts = None
        for row in rows:
            pt = self._history_point_from_row(row["ts"], row["payload"])
            if not pt:
                continue
            if first_ts is None:
                first_ts = pt["ts"]
            last_ts = pt["ts"]
            accel_util = pt.get("accel_util_avg")
            if accel_util is None:
                accel_util = pt.get("npu_util_avg")
            if accel_util is None:
                accel_util = pt.get("gpu_util_avg")
            accel_temp = pt.get("accel_temp_max")
            if accel_temp is None:
                accel_temp = pt.get("npu_temp_max")
            if accel_temp is None:
                accel_temp = pt.get("gpu_temp_max")
            values = {
                "cpu_percent": pt.get("cpu_percent"),
                "mem_percent": pt.get("mem_percent"),
                "disk_percent": pt.get("disk_percent"),
                "load1": pt.get("load1"),
                "net_rx_mbps": pt.get("net_rx_mbps"),
                "net_tx_mbps": pt.get("net_tx_mbps"),
                "accel_util_avg": accel_util,
                "accel_temp_max": accel_temp,
            }
            for key, raw in values.items():
                if raw is None:
                    continue
                try:
                    series[key].append(float(raw))
                except (TypeError, ValueError):
                    pass

        def _agg(nums: List[float]) -> Dict[str, Optional[float]]:
            if not nums:
                return {"avg": None, "min": None, "max": None}
            return {
                "avg": round(sum(nums) / len(nums), 2),
                "min": round(min(nums), 2),
                "max": round(max(nums), 2),
            }

        return {
            "host_id": host_id,
            "minutes": minutes,
            "sample_count": len(rows),
            "from_ts": first_ts,
            "to_ts": last_ts,
            "metrics": {
                "cpu_percent": _agg(series["cpu_percent"]),
                "mem_percent": _agg(series["mem_percent"]),
                "disk_percent": _agg(series["disk_percent"]),
                "load1": _agg(series["load1"]),
                "net_rx_mbps": _agg(series["net_rx_mbps"]),
                "net_tx_mbps": _agg(series["net_tx_mbps"]),
                "accel_util_avg": _agg(series["accel_util_avg"]),
                "accel_temp_max": _agg(series["accel_temp_max"]),
            },
        }

    def export_hosts_rows(self) -> List[Dict[str, Any]]:
        return self.list_hosts()

    def cleanup_old_metrics(self) -> int:
        if self.retention_days <= 0:
            return 0
        cutoff = int(time.time()) - self.retention_days * 86400
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute("DELETE FROM metrics_history WHERE ts < ?", (cutoff,))
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()
