"""SQLite 存储：主机快照与历史上报。"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple


PERIOD_METRIC_KEYS = (
    "cpu_percent",
    "mem_percent",
    "disk_percent",
    "load1",
    "net_rx_mbps",
    "net_tx_mbps",
    "net_rated_mbps",
    "net_rx_percent",
    "net_tx_percent",
    "accel_util_avg",
    "accel_temp_max",
)

DEFAULT_BUSY_THRESHOLDS = {
    "cpu_percent": 80.0,
    "accel_util_avg": 80.0,
}

# 单机查询安全上限：约 7 天 × 10s 采样
PERIOD_MAX_ROWS = 60000


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ratio_percent(part: Any, total: Any) -> Optional[float]:
    p = _to_float(part)
    t = _to_float(total)
    if p is None or t is None or t <= 0:
        return None
    return round(p * 100.0 / t, 2)


def percentile(nums: List[float], p: float = 95.0) -> Optional[float]:
    """线性插值分位数。rank = (p/100) * (n-1)。"""
    if not nums:
        return None
    if p <= 0:
        return round(min(nums), 2)
    if p >= 100:
        return round(max(nums), 2)
    ordered = sorted(nums)
    n = len(ordered)
    if n == 1:
        return round(ordered[0], 2)
    rank = (float(p) / 100.0) * (n - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return round(ordered[lo], 2)
    weight = rank - lo
    return round(ordered[lo] * (1.0 - weight) + ordered[hi] * weight, 2)


def aggregate_series(
    nums: List[float],
    busy_threshold: Optional[float] = None,
) -> Dict[str, Optional[float]]:
    """avg / min / max / p95；busy_ratio 为 >= 阈值的样本占比。"""
    if not nums:
        return {
            "avg": None,
            "min": None,
            "max": None,
            "p95": None,
            "busy_ratio": None,
            "sample_count": 0,
        }
    out: Dict[str, Optional[float]] = {
        "avg": round(sum(nums) / len(nums), 2),
        "min": round(min(nums), 2),
        "max": round(max(nums), 2),
        "p95": percentile(nums, 95.0),
        "busy_ratio": None,
        "sample_count": len(nums),
    }
    if busy_threshold is not None:
        busy = sum(1 for x in nums if x >= busy_threshold)
        out["busy_ratio"] = round(busy / float(len(nums)), 4)
    return out


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
        """历史上报只保留趋势/时段统计所需字段，减小库体积。"""
        system = payload.get("system") or {}
        npus = payload.get("npus") or []
        gpus = payload.get("gpus") or []
        rated = system.get("net_rated_mbps")
        rx_pct = system.get("net_rx_percent")
        tx_pct = system.get("net_tx_percent")
        if rx_pct is None:
            rx_pct = _ratio_percent(system.get("net_rx_mbps"), rated)
        if tx_pct is None:
            tx_pct = _ratio_percent(system.get("net_tx_mbps"), rated)
        return {
            "host_id": payload.get("host_id"),
            "timestamp": payload.get("timestamp"),
            "system": {
                "cpu_percent": system.get("cpu_percent"),
                "cpu_count": system.get("cpu_count"),
                "cpu_freq_mhz": system.get("cpu_freq_mhz"),
                "cpu_freq_max_mhz": system.get("cpu_freq_max_mhz"),
                "mem_percent": system.get("mem_percent"),
                "mem_used_mb": system.get("mem_used_mb"),
                "mem_total_mb": system.get("mem_total_mb"),
                "disk_percent": system.get("disk_percent"),
                "disk_used_gb": system.get("disk_used_gb"),
                "disk_total_gb": system.get("disk_total_gb"),
                "load1": system.get("load1"),
                "load5": system.get("load5"),
                "load15": system.get("load15"),
                "net_rx_mbps": system.get("net_rx_mbps"),
                "net_tx_mbps": system.get("net_tx_mbps"),
                "net_rated_mbps": rated,
                "net_link_mbps": system.get("net_link_mbps"),
                "net_rx_percent": rx_pct,
                "net_tx_percent": tx_pct,
            },
            "npus": [
                {
                    "index": n.get("index"),
                    "util_percent": n.get("util_percent"),
                    "temp_c": n.get("temp_c"),
                    "mem_percent": n.get("mem_percent"),
                    "mem_used_mb": n.get("mem_used_mb"),
                    "mem_total_mb": n.get("mem_total_mb"),
                    "power_w": n.get("power_w"),
                    "health": n.get("health"),
                }
                for n in npus
            ],
            "gpus": [
                {
                    "index": g.get("index"),
                    "util_percent": g.get("util_percent"),
                    "temp_c": g.get("temp_c"),
                    "mem_percent": g.get("mem_percent"),
                    "mem_used_mb": g.get("mem_used_mb"),
                    "mem_total_mb": g.get("mem_total_mb"),
                    "power_w": g.get("power_w"),
                    "power_limit_w": g.get("power_limit_w"),
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
            "net_rx_mbps": system.get("net_rx_mbps"),
            "net_tx_mbps": system.get("net_tx_mbps"),
            "net_rated_mbps": system.get("net_rated_mbps"),
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
        rated = system.get("net_rated_mbps")
        rx_pct = system.get("net_rx_percent")
        tx_pct = system.get("net_tx_percent")
        if rx_pct is None:
            rx_pct = _ratio_percent(system.get("net_rx_mbps"), rated)
        if tx_pct is None:
            tx_pct = _ratio_percent(system.get("net_tx_mbps"), rated)
        return {
            "ts": ts,
            "cpu_percent": system.get("cpu_percent"),
            "mem_percent": system.get("mem_percent"),
            "disk_percent": system.get("disk_percent"),
            "load1": system.get("load1"),
            "net_rx_mbps": system.get("net_rx_mbps"),
            "net_tx_mbps": system.get("net_tx_mbps"),
            "net_rated_mbps": rated,
            "net_rx_percent": rx_pct,
            "net_tx_percent": tx_pct,
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

    def _retention_span_seconds(self) -> int:
        days = int(self.retention_days or 0)
        if days <= 0:
            return 7 * 86400
        return days * 86400

    def resolve_period_window(
        self,
        minutes: Optional[int] = None,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        now: Optional[int] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """解析相对 minutes 或绝对 from_ts/to_ts，并裁剪到保留期。

        同时提供绝对范围时以 from_ts/to_ts 为准。
        成功返回 (None, window)；失败返回 (error, {})。
        """
        now_ts = int(now if now is not None else time.time())
        retention_sec = self._retention_span_seconds()
        earliest = now_ts - retention_sec
        max_minutes = max(1, retention_sec // 60)

        if from_ts is not None or to_ts is not None:
            if from_ts is None or to_ts is None:
                return "from_ts 与 to_ts 必须同时提供", {}
            try:
                req_from = int(from_ts)
                req_to = int(to_ts)
            except (TypeError, ValueError):
                return "from_ts / to_ts 必须是整数时间戳", {}
            if req_from >= req_to:
                return "from_ts 必须小于 to_ts", {}
            clamped = False
            win_from = req_from
            win_to = req_to
            if win_from < earliest:
                win_from = earliest
                clamped = True
            if win_to > now_ts:
                win_to = now_ts
                clamped = True
            if win_from >= win_to:
                return "时间范围已超出历史保留期", {}
            span_minutes = max(1, int(round((win_to - win_from) / 60.0)))
            return None, {
                "mode": "absolute",
                "minutes": span_minutes,
                "from_ts": win_from,
                "to_ts": win_to,
                "requested_from_ts": req_from,
                "requested_to_ts": req_to,
                "requested_minutes": None,
                "retention_days": int(self.retention_days or 7),
                "clamped": clamped,
                "now": now_ts,
            }

        if minutes is None:
            minutes = 120
        try:
            req_minutes = int(minutes)
        except (TypeError, ValueError):
            return "minutes 必须是整数", {}
        win_minutes = max(1, min(req_minutes, max_minutes))
        clamped = win_minutes != req_minutes
        win_from = now_ts - win_minutes * 60
        return None, {
            "mode": "relative",
            "minutes": win_minutes,
            "from_ts": win_from,
            "to_ts": now_ts,
            "requested_from_ts": None,
            "requested_to_ts": None,
            "requested_minutes": req_minutes,
            "retention_days": int(self.retention_days or 7),
            "clamped": clamped,
            "now": now_ts,
        }

    @staticmethod
    def _apply_rated_fallback(
        point: Dict[str, Any],
        rated_fallback: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        fallback = rated_fallback or {}
        rated = point.get("net_rated_mbps")
        if rated is None:
            rated = fallback.get("net_rated_mbps")
            point["net_rated_mbps"] = rated
        if point.get("net_rx_percent") is None:
            point["net_rx_percent"] = _ratio_percent(point.get("net_rx_mbps"), rated)
        if point.get("net_tx_percent") is None:
            point["net_tx_percent"] = _ratio_percent(point.get("net_tx_mbps"), rated)
        return point

    @staticmethod
    def _rated_fallback_from_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        system = (payload or {}).get("system") or {}
        return {"net_rated_mbps": system.get("net_rated_mbps")}

    @staticmethod
    def _accel_from_point(pt: Dict[str, Any]) -> Tuple[Any, Any]:
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
        return accel_util, accel_temp

    def host_history(
        self,
        host_id: str,
        minutes: int = 60,
        limit: int = 240,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        rated_fallback: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """返回主机历史精简序列，供趋势图使用（取时间窗内最近的点）。"""
        err, window = self.resolve_period_window(
            minutes=minutes, from_ts=from_ts, to_ts=to_ts
        )
        if err:
            return []
        limit = max(1, min(int(limit), 2000))
        since = int(window["from_ts"])
        until = int(window["to_ts"])

        with self._lock:
            conn = self._connect()
            try:
                # 先取最近 limit 条，再按时间正序返回，避免长窗口只拿到最旧点
                rows = conn.execute(
                    """
                    SELECT ts, payload FROM (
                      SELECT ts, payload FROM metrics_history
                      WHERE host_id=? AND ts>=? AND ts<=?
                      ORDER BY ts DESC
                      LIMIT ?
                    ) ORDER BY ts ASC
                    """,
                    (host_id, since, until, limit),
                ).fetchall()
            finally:
                conn.close()

        points: List[Dict[str, Any]] = []
        for row in rows:
            pt = self._history_point_from_row(row["ts"], row["payload"])
            if pt:
                points.append(self._apply_rated_fallback(pt, rated_fallback))
        return points

    def _fetch_history_rows(
        self,
        host_id: str,
        from_ts: int,
        to_ts: int,
        max_rows: int = PERIOD_MAX_ROWS,
    ) -> List[Any]:
        with self._lock:
            conn = self._connect()
            try:
                return conn.execute(
                    """
                    SELECT ts, payload FROM metrics_history
                    WHERE host_id=? AND ts>=? AND ts<=?
                    ORDER BY ts ASC
                    LIMIT ?
                    """,
                    (host_id, int(from_ts), int(to_ts), int(max_rows)),
                ).fetchall()
            finally:
                conn.close()

    def _series_from_rows(
        self,
        rows: List[Any],
        rated_fallback: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Dict[str, List[float]], Optional[int], Optional[int], int]:
        series: Dict[str, List[float]] = {key: [] for key in PERIOD_METRIC_KEYS}
        first_ts = None
        last_ts = None
        parsed = 0
        for row in rows:
            pt = self._history_point_from_row(row["ts"], row["payload"])
            if not pt:
                continue
            pt = self._apply_rated_fallback(pt, rated_fallback)
            parsed += 1
            if first_ts is None:
                first_ts = pt["ts"]
            last_ts = pt["ts"]
            accel_util, accel_temp = self._accel_from_point(pt)
            values = {
                "cpu_percent": pt.get("cpu_percent"),
                "mem_percent": pt.get("mem_percent"),
                "disk_percent": pt.get("disk_percent"),
                "load1": pt.get("load1"),
                "net_rx_mbps": pt.get("net_rx_mbps"),
                "net_tx_mbps": pt.get("net_tx_mbps"),
                "net_rated_mbps": pt.get("net_rated_mbps"),
                "net_rx_percent": pt.get("net_rx_percent"),
                "net_tx_percent": pt.get("net_tx_percent"),
                "accel_util_avg": accel_util,
                "accel_temp_max": accel_temp,
            }
            for key, raw in values.items():
                num = _to_float(raw)
                if num is None:
                    continue
                series[key].append(num)
        return series, first_ts, last_ts, parsed

    @staticmethod
    def _metrics_from_series(
        series: Dict[str, List[float]],
        busy_thresholds: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Dict[str, Optional[float]]]:
        thresholds = busy_thresholds or {}
        return {
            key: aggregate_series(series.get(key) or [], thresholds.get(key))
            for key in PERIOD_METRIC_KEYS
        }

    def host_period_stats(
        self,
        host_id: str,
        minutes: int = 120,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        busy_thresholds: Optional[Dict[str, float]] = None,
        rated_fallback: Optional[Dict[str, Any]] = None,
        window: Optional[Dict[str, Any]] = None,
        include_series: bool = False,
    ) -> Dict[str, Any]:
        """对时间窗内全部历史点做平均/最低/最高/P95/繁忙占比统计。"""
        if window is None:
            err, window = self.resolve_period_window(
                minutes=minutes, from_ts=from_ts, to_ts=to_ts
            )
            if err:
                return {
                    "host_id": host_id,
                    "ok": False,
                    "error": err,
                    "minutes": minutes,
                    "sample_count": 0,
                    "from_ts": None,
                    "to_ts": None,
                    "metrics": self._metrics_from_series(
                        {key: [] for key in PERIOD_METRIC_KEYS}, busy_thresholds
                    ),
                }
        host = None
        if rated_fallback is None:
            host = self.get_host(host_id)
            rated_fallback = self._rated_fallback_from_payload(
                (host or {}).get("payload")
            )
        rows = self._fetch_history_rows(
            host_id, int(window["from_ts"]), int(window["to_ts"])
        )
        series, first_ts, last_ts, parsed = self._series_from_rows(
            rows, rated_fallback=rated_fallback
        )
        if busy_thresholds is None:
            thresholds = dict(DEFAULT_BUSY_THRESHOLDS)
        else:
            thresholds = dict(busy_thresholds)
        hostname = None
        host_type = None
        if host is None:
            host = self.get_host(host_id)
        if host:
            hostname = host.get("hostname")
            host_type = host.get("host_type")
        result = {
            "host_id": host_id,
            "hostname": hostname,
            "host_type": host_type,
            "minutes": window["minutes"],
            "sample_count": parsed,
            "from_ts": first_ts,
            "to_ts": last_ts,
            "window": window,
            "busy_thresholds": thresholds,
            "metrics": self._metrics_from_series(series, thresholds),
        }
        if include_series:
            result["_series"] = series
        return result

    def cluster_period_stats(
        self,
        minutes: int = 120,
        from_ts: Optional[int] = None,
        to_ts: Optional[int] = None,
        host_ids: Optional[List[str]] = None,
        busy_thresholds: Optional[Dict[str, float]] = None,
        window: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """多机时段利用：每主机聚合 + 样本加权集群汇总。"""
        if window is None:
            err, window = self.resolve_period_window(
                minutes=minutes, from_ts=from_ts, to_ts=to_ts
            )
            if err:
                return {"ok": False, "error": err}
        if busy_thresholds is None:
            thresholds = dict(DEFAULT_BUSY_THRESHOLDS)
        else:
            thresholds = dict(busy_thresholds)
        hosts = self.list_hosts()
        if host_ids:
            wanted = {h.strip() for h in host_ids if h and h.strip()}
            hosts = [h for h in hosts if h.get("host_id") in wanted]

        pooled: Dict[str, List[float]] = {key: [] for key in PERIOD_METRIC_KEYS}
        host_rows: List[Dict[str, Any]] = []
        total_samples = 0
        hosts_with_samples = 0
        for host in hosts:
            hid = host["host_id"]
            rated_fallback = None
            detail = self.get_host(hid)
            if detail:
                rated_fallback = self._rated_fallback_from_payload(detail.get("payload"))
            stats = self.host_period_stats(
                hid,
                minutes=window["minutes"],
                from_ts=window["from_ts"],
                to_ts=window["to_ts"],
                busy_thresholds=thresholds,
                rated_fallback=rated_fallback,
                window=window,
                include_series=True,
            )
            series = stats.pop("_series", {}) or {}
            for key in PERIOD_METRIC_KEYS:
                pooled[key].extend(series.get(key) or [])
            total_samples += int(stats.get("sample_count") or 0)
            if stats.get("sample_count"):
                hosts_with_samples += 1
            host_rows.append(
                {
                    "host_id": hid,
                    "hostname": host.get("hostname") or hid,
                    "host_type": host.get("host_type"),
                    "online": host.get("online"),
                    "sample_count": stats.get("sample_count") or 0,
                    "from_ts": stats.get("from_ts"),
                    "to_ts": stats.get("to_ts"),
                    "metrics": stats.get("metrics") or {},
                }
            )

        return {
            "minutes": window["minutes"],
            "window": window,
            "rollup": "sample_weighted",
            "rollup_note": "集群 avg/p95/busy_ratio 按全部主机样本合并后计算（样本多的主机权重大）；min/max 取全局最低/最高。",
            "busy_thresholds": thresholds,
            "host_count": len(host_rows),
            "hosts_with_samples": hosts_with_samples,
            "sample_count": total_samples,
            "from_ts": window["from_ts"],
            "to_ts": window["to_ts"],
            "cluster": {
                "sample_count": total_samples,
                "metrics": self._metrics_from_series(pooled, thresholds),
            },
            "hosts": host_rows,
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
