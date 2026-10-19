"""SQLite 存储：主机快照与历史上报。"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from center.hostaddr import normalize_ip, resolve_host_address
from center.hostmeta import (
    ASSET_FIELDS,
    collected_hardware,
    dump_asset_meta,
    host_match_ips,
    parse_asset_meta,
    parse_previous_ips,
)
from common.accel_identity import (
    accel_utils,
    cards_from_payload,
    merge_inventories,
    summarize_accelerators,
)


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


# 利用率只接受 0～100%。负值、>100% 和非有限数（例如昇腾 310P3 曾上报到百万级）
# 不能进入平均 / P95 / 繁忙占比。缺省值不算「排除」。
UTIL_PERCENT_MIN = 0.0
UTIL_PERCENT_MAX = 100.0


def classify_util_percent(value: Any) -> Tuple[Optional[float], bool]:
    """返回 (可用百分比或 None, 是否因越界被排除)。"""
    if value is None or value == "":
        return None, False
    num = _to_float(value)
    if num is None:
        return None, False
    if (
        not math.isfinite(num)
        or num < UTIL_PERCENT_MIN
        or num > UTIL_PERCENT_MAX
    ):
        return None, True
    return num, False


def _slim_disk_history(system: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """历史里只留挂载、利用率、容量，供诊断报告指出最满挂载。

    没有 disks[] 时返回 None（旧 Agent 不写该键）。device/fstype 不入库。
    """
    raw = system.get("disks")
    if not isinstance(raw, list):
        return None
    slim: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        slim.append(
            {
                "mount": item.get("mount") or "",
                "percent": _to_float(item.get("percent")),
                "used_gb": _to_float(item.get("used_gb")),
                "total_gb": _to_float(item.get("total_gb")),
            }
        )
    return slim


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
                        created_at INTEGER NOT NULL,
                        last_remote_ip TEXT
                    );
                    CREATE TABLE IF NOT EXISTS metrics_history (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        host_id TEXT NOT NULL,
                        ts INTEGER NOT NULL,
                        payload TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS idx_metrics_host_ts
                        ON metrics_history(host_id, ts);
                    CREATE TABLE IF NOT EXISTS resource_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL UNIQUE,
                        created_at INTEGER NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS host_group_members (
                        host_id TEXT PRIMARY KEY,
                        group_id INTEGER NOT NULL,
                        FOREIGN KEY(group_id) REFERENCES resource_groups(id)
                    );
                    CREATE INDEX IF NOT EXISTS idx_host_group_members_gid
                        ON host_group_members(group_id);
                    CREATE TABLE IF NOT EXISTS diagnostic_reports (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        period_type TEXT NOT NULL,
                        title TEXT NOT NULL,
                        requested_from_ts INTEGER NOT NULL,
                        requested_to_ts INTEGER NOT NULL,
                        clamped INTEGER NOT NULL DEFAULT 0,
                        created_at INTEGER NOT NULL,
                        markdown TEXT NOT NULL,
                        payload TEXT NOT NULL,
                        source TEXT NOT NULL DEFAULT 'manual',
                        status TEXT NOT NULL DEFAULT 'ok',
                        error TEXT,
                        generated_at INTEGER
                    );
                    CREATE INDEX IF NOT EXISTS idx_diag_reports_created
                        ON diagnostic_reports(created_at DESC, id DESC);
                    CREATE INDEX IF NOT EXISTS idx_diag_reports_window
                        ON diagnostic_reports(period_type, requested_from_ts, requested_to_ts);
                    """
                )
                cols = {
                    r[1] for r in conn.execute("PRAGMA table_info(hosts)").fetchall()
                }
                if "last_remote_ip" not in cols:
                    conn.execute("ALTER TABLE hosts ADD COLUMN last_remote_ip TEXT")
                # 不加 NOT NULL DEFAULT：SQLite 3.37 之前非空默认值会重写整表。
                # NULL 表示未隐藏。只改 hosts，不动 metrics_history。
                if "hidden" not in cols:
                    conn.execute("ALTER TABLE hosts ADD COLUMN hidden INTEGER")
                if "hidden_at" not in cols:
                    conn.execute("ALTER TABLE hosts ADD COLUMN hidden_at INTEGER")
                if "asset_meta" not in cols:
                    conn.execute("ALTER TABLE hosts ADD COLUMN asset_meta TEXT")
                # 诊断报告列只做加法。不改 metrics_history，避免在大库上重写历史上报。
                diag_cols = {
                    r[1]
                    for r in conn.execute(
                        "PRAGMA table_info(diagnostic_reports)"
                    ).fetchall()
                }
                diag_alters = (
                    (
                        "source",
                        "ALTER TABLE diagnostic_reports ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'",
                    ),
                    (
                        "status",
                        "ALTER TABLE diagnostic_reports ADD COLUMN status TEXT NOT NULL DEFAULT 'ok'",
                    ),
                    ("error", "ALTER TABLE diagnostic_reports ADD COLUMN error TEXT"),
                    (
                        "generated_at",
                        "ALTER TABLE diagnostic_reports ADD COLUMN generated_at INTEGER",
                    ),
                )
                for name, sql in diag_alters:
                    if name not in diag_cols:
                        conn.execute(sql)
                conn.execute(
                    """
                    UPDATE diagnostic_reports
                    SET generated_at = created_at
                    WHERE generated_at IS NULL
                    """
                )
                conn.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_diag_reports_window
                        ON diagnostic_reports(period_type, requested_from_ts, requested_to_ts)
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def upsert_metric(
        self,
        payload: Dict[str, Any],
        remote_ip: Optional[str] = None,
    ) -> None:
        host_id = payload["host_id"]
        hostname = payload.get("hostname") or host_id
        host_type = payload.get("host_type") or "cpu"
        ts = int(payload.get("timestamp") or time.time())
        raw = json.dumps(payload, ensure_ascii=False)
        compact = json.dumps(self._compact_history_payload(payload), ensure_ascii=False)
        now = int(time.time())
        system = payload.get("system") or {}
        if not isinstance(system, dict):
            system = {}
        # normalize_ip 会丢掉回环 / 169.254 / 198.18/15（Clash TUN 假 IP）
        stored_ip = (
            normalize_ip(system.get("primary_ip"))
            or normalize_ip(system.get("ipv4"))
            or normalize_ip(system.get("ip"))
            or normalize_ip(remote_ip)
            or None
        )

        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO hosts (
                        host_id, hostname, host_type, last_seen, last_payload,
                        created_at, last_remote_ip
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(host_id) DO UPDATE SET
                        hostname=excluded.hostname,
                        host_type=excluded.host_type,
                        last_seen=excluded.last_seen,
                        last_payload=excluded.last_payload,
                        last_remote_ip=COALESCE(excluded.last_remote_ip, hosts.last_remote_ip)
                    """,
                    (host_id, hostname, host_type, ts, raw, now, stored_ip),
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
        system_out: Dict[str, Any] = {
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
        }
        slim_disks = _slim_disk_history(system)
        if slim_disks is not None:
            system_out["disks"] = slim_disks
        return {
            "host_id": payload.get("host_id"),
            "timestamp": payload.get("timestamp"),
            "system": system_out,
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
    def _clean_group_name(name: Any) -> Tuple[Optional[str], Optional[str]]:
        text = str(name or "").strip()
        if not text:
            return None, "资源组名称不能为空"
        if len(text) > 40:
            return None, "资源组名称不能超过 40 个字符"
        return text, None

    def _deploy_ip_map(self, conn: sqlite3.Connection) -> Dict[str, str]:
        out: Dict[str, str] = {}
        rows = None
        for sql in (
            # 已停用的目标不再驱动展示地址。旧库没有 retired 列时退回全表。
            "SELECT host_id, hostname, ip FROM deploy_targets WHERE COALESCE(retired, 0) = 0",
            "SELECT host_id, hostname, ip FROM deploy_targets",
        ):
            try:
                rows = conn.execute(sql).fetchall()
                break
            except sqlite3.OperationalError:
                rows = None
        if rows is None:
            return out
        for row in rows:
            ip = normalize_ip(row["ip"])
            if not ip:
                continue
            hid = str(row["host_id"] or "").strip()
            hn = str(row["hostname"] or "").strip()
            if hid and hid not in out:
                out[hid] = ip
            if hn and hn not in out:
                out[hn] = ip
            out[ip] = ip
        return out

    def _host_group_map(self, conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        try:
            rows = conn.execute(
                """
                SELECT m.host_id, m.group_id, g.name
                FROM host_group_members m
                JOIN resource_groups g ON g.id = m.group_id
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return out
        for row in rows:
            out[str(row["host_id"])] = {
                "group_id": int(row["group_id"]),
                "group_name": row["name"],
            }
        return out

    def _attach_host_meta(
        self,
        item: Dict[str, Any],
        payload: Optional[Dict[str, Any]],
        deploy_map: Optional[Dict[str, str]] = None,
        group_map: Optional[Dict[str, Dict[str, Any]]] = None,
        remote_ip: Any = "",
    ) -> Dict[str, Any]:
        hid = item.get("host_id")
        item["address"] = resolve_host_address(
            host_id=hid,
            hostname=item.get("hostname"),
            payload=payload,
            remote_ip=remote_ip or item.get("last_remote_ip"),
            deploy_map=deploy_map,
        )
        meta = (group_map or {}).get(str(hid or "")) or {}
        item["group_id"] = meta.get("group_id")
        item["group_name"] = meta.get("group_name")
        return item

    def list_groups(self) -> List[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT g.id, g.name, g.created_at,
                           COUNT(h.host_id) AS host_count
                    FROM resource_groups g
                    LEFT JOIN host_group_members m ON m.group_id = g.id
                    LEFT JOIN hosts h
                      ON h.host_id = m.host_id AND COALESCE(h.hidden, 0) = 0
                    GROUP BY g.id
                    ORDER BY g.name COLLATE NOCASE
                    """
                ).fetchall()
            finally:
                conn.close()
        return [
            {
                "id": int(row["id"]),
                "name": row["name"],
                "created_at": row["created_at"],
                "host_count": int(row["host_count"] or 0),
            }
            for row in rows
        ]

    def create_group(self, name: Any) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        cleaned, err = self._clean_group_name(name)
        if err:
            return err, None
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                try:
                    cur = conn.execute(
                        "INSERT INTO resource_groups (name, created_at) VALUES (?, ?)",
                        (cleaned, now),
                    )
                    conn.commit()
                    gid = int(cur.lastrowid)
                except sqlite3.IntegrityError:
                    return "资源组名称已存在", None
            finally:
                conn.close()
        return None, {
            "id": gid,
            "name": cleaned,
            "created_at": now,
            "host_count": 0,
        }

    def rename_group(
        self, group_id: int, name: Any
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        cleaned, err = self._clean_group_name(name)
        if err:
            return err, None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT id FROM resource_groups WHERE id=?", (int(group_id),)
                ).fetchone()
                if not row:
                    return "资源组不存在", None
                try:
                    conn.execute(
                        "UPDATE resource_groups SET name=? WHERE id=?",
                        (cleaned, int(group_id)),
                    )
                    conn.commit()
                except sqlite3.IntegrityError:
                    return "资源组名称已存在", None
            finally:
                conn.close()
        groups = {g["id"]: g for g in self.list_groups()}
        return None, groups.get(int(group_id))

    def delete_group(self, group_id: int) -> bool:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "DELETE FROM host_group_members WHERE group_id=?", (int(group_id),)
                )
                cur = conn.execute(
                    "DELETE FROM resource_groups WHERE id=?", (int(group_id),)
                )
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    def assign_host_group(
        self, host_id: str, group_id: Optional[int]
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        hid = str(host_id or "").strip()
        if not hid:
            return "host_id 不能为空", None
        with self._lock:
            conn = self._connect()
            try:
                host = conn.execute(
                    "SELECT host_id FROM hosts WHERE host_id=?", (hid,)
                ).fetchone()
                if not host:
                    return "主机不存在", None
                if group_id is None or group_id == "":
                    conn.execute(
                        "DELETE FROM host_group_members WHERE host_id=?", (hid,)
                    )
                    conn.commit()
                    return None, {"host_id": hid, "group_id": None, "group_name": None}
                gid = int(group_id)
                row = conn.execute(
                    "SELECT id, name FROM resource_groups WHERE id=?", (gid,)
                ).fetchone()
                if not row:
                    return "资源组不存在", None
                conn.execute(
                    """
                    INSERT INTO host_group_members (host_id, group_id)
                    VALUES (?, ?)
                    ON CONFLICT(host_id) DO UPDATE SET group_id=excluded.group_id
                    """,
                    (hid, gid),
                )
                conn.commit()
                return None, {
                    "host_id": hid,
                    "group_id": gid,
                    "group_name": row["name"],
                }
            finally:
                conn.close()

    @staticmethod
    def _summary_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        system = payload.get("system") or {}
        npus = payload.get("npus") or []
        gpus = payload.get("gpus") or []
        npu_utils = []
        for n in npus:
            if not isinstance(n, dict):
                continue
            num, _bad = classify_util_percent(n.get("util_percent"))
            if num is not None:
                npu_utils.append(num)
        gpu_utils = []
        for g in gpus:
            if not isinstance(g, dict):
                continue
            num, _bad = classify_util_percent(g.get("util_percent"))
            if num is not None:
                gpu_utils.append(num)
        cards = cards_from_payload(payload)
        identity = summarize_accelerators(cards)
        accel_util_vals = accel_utils(cards)
        disks = system.get("disks")
        disk_count = system.get("disk_count")
        if disk_count is None and isinstance(disks, list):
            disk_count = len(disks)
        return {
            "cpu_percent": classify_util_percent(system.get("cpu_percent"))[0],
            "cpu_count": system.get("cpu_count"),
            "mem_percent": classify_util_percent(system.get("mem_percent"))[0],
            "disk_percent": classify_util_percent(system.get("disk_percent"))[0],
            "disk_used_gb": system.get("disk_used_gb"),
            "disk_total_gb": system.get("disk_total_gb"),
            "disk_count": disk_count,
            "load1": system.get("load1"),
            "net_rx_mbps": system.get("net_rx_mbps"),
            "net_tx_mbps": system.get("net_tx_mbps"),
            "net_rated_mbps": system.get("net_rated_mbps"),
            "npu_count": len(npus),
            "npu_util_avg": round(sum(npu_utils) / len(npu_utils), 2) if npu_utils else None,
            "gpu_count": len(gpus),
            "gpu_util_avg": round(sum(gpu_utils) / len(gpu_utils), 2) if gpu_utils else None,
            "accel_count": identity["count"],
            "accel_summary": identity["summary"] or None,
            "accel_inventory": identity["inventory"],
            "accel_util_avg": (
                round(sum(accel_util_vals) / len(accel_util_vals), 2)
                if accel_util_vals
                else None
            ),
        }

    @staticmethod
    def _row_flag(row: sqlite3.Row, key: str, default: int = 0) -> int:
        try:
            value = row[key]
        except (IndexError, KeyError):
            return default
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _decorate_host_item(
        self,
        item: Dict[str, Any],
        row: sqlite3.Row,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        asset = parse_asset_meta(self._row_text(row, "asset_meta"))
        hidden = self._row_flag(row, "hidden") == 1
        hidden_at = self._row_optional_int(row, "hidden_at")
        item["hidden"] = hidden
        item["hidden_at"] = hidden_at
        item["asset"] = asset
        item["owner"] = asset.get("owner") or ""
        item["purpose"] = asset.get("purpose") or ""
        item["previous_ips"] = list(asset.get("previous_ips") or [])
        if asset.get("readdress_undo"):
            item["readdress_undo"] = asset["readdress_undo"]
        manual = str(asset.get("address") or "").strip()
        if normalize_ip(manual):
            item["address"] = resolve_host_address(
                host_id=item.get("host_id"),
                hostname=item.get("hostname"),
                payload=payload,
                remote_ip=item.get("last_remote_ip"),
                manual_address=manual,
            )
        item["hardware"] = collected_hardware(payload)
        item["ips"] = host_match_ips(item, payload)
        return item

    @staticmethod
    def _row_text(row: sqlite3.Row, key: str) -> str:
        try:
            value = row[key]
        except (IndexError, KeyError):
            return ""
        return "" if value is None else str(value)

    @staticmethod
    def _row_optional_int(row: sqlite3.Row, key: str) -> Optional[int]:
        try:
            value = row[key]
        except (IndexError, KeyError):
            return None
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def list_hosts(
        self,
        include_hidden: bool = False,
        hidden_only: bool = False,
    ) -> List[Dict[str, Any]]:
        now = int(time.time())
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT host_id, hostname, host_type, last_seen, last_payload,
                           last_remote_ip, hidden, hidden_at, asset_meta
                    FROM hosts ORDER BY host_id
                    """
                ).fetchall()
                deploy_map = self._deploy_ip_map(conn)
                group_map = self._host_group_map(conn)
            finally:
                conn.close()

        result: List[Dict[str, Any]] = []
        for row in rows:
            hidden = self._row_flag(row, "hidden") == 1
            if hidden_only and not hidden:
                continue
            if hidden and not include_hidden and not hidden_only:
                continue
            payload = json.loads(row["last_payload"])
            item = {
                "host_id": row["host_id"],
                "hostname": row["hostname"],
                "host_type": row["host_type"],
                "last_seen": row["last_seen"],
                "online": self._is_online(row["last_seen"], now),
                "last_remote_ip": row["last_remote_ip"],
            }
            item.update(self._summary_from_payload(payload))
            self._attach_host_meta(
                item,
                payload,
                deploy_map=deploy_map,
                group_map=group_map,
                remote_ip=row["last_remote_ip"],
            )
            self._decorate_host_item(item, row, payload)
            result.append(item)
        return result

    def get_host(self, host_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT host_id, hostname, host_type, last_seen, last_payload,
                           created_at, last_remote_ip, hidden, hidden_at, asset_meta
                    FROM hosts WHERE host_id=?
                    """,
                    (host_id,),
                ).fetchone()
                deploy_map = self._deploy_ip_map(conn)
                group_map = self._host_group_map(conn)
            finally:
                conn.close()
        if not row:
            return None
        payload = json.loads(row["last_payload"])
        item = {
            "host_id": row["host_id"],
            "hostname": row["hostname"],
            "host_type": row["host_type"],
            "last_seen": row["last_seen"],
            "created_at": row["created_at"],
            "online": self._is_online(row["last_seen"]),
            "last_remote_ip": row["last_remote_ip"],
            "payload": payload,
        }
        identity = summarize_accelerators(cards_from_payload(payload))
        item["accel_count"] = identity["count"]
        item["accel_summary"] = identity["summary"] or None
        item["accel_inventory"] = identity["inventory"]
        self._attach_host_meta(
            item,
            payload,
            deploy_map=deploy_map,
            group_map=group_map,
            remote_ip=row["last_remote_ip"],
        )
        self._decorate_host_item(item, row, payload)
        return item

    def update_host_asset(
        self,
        host_id: str,
        patch: Dict[str, str],
        previous_ips: Optional[List[Dict[str, str]]] = None,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """合并手工资产字段。patch 里的空字符串会清空。不上报路径调用。

        ``previous_ips is None`` 表示不改历史 IP。传入列表（含空列表）则整表替换。
        """
        hid = str(host_id or "").strip()
        if not hid:
            return "host_id 不能为空", None
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT asset_meta FROM hosts WHERE host_id=?", (hid,)
                ).fetchone()
                if not row:
                    return "主机不存在", None
                asset = parse_asset_meta(row["asset_meta"])
                for key, value in patch.items():
                    if key in ASSET_FIELDS:
                        asset[key] = value
                if previous_ips is not None:
                    asset["previous_ips"] = parse_previous_ips(previous_ips)
                conn.execute(
                    "UPDATE hosts SET asset_meta=? WHERE host_id=?",
                    (dump_asset_meta(asset), hid),
                )
                conn.commit()
            finally:
                conn.close()
        return None, asset

    def history_count(self, host_id: str) -> int:
        hid = str(host_id or "").strip()
        if not hid:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM metrics_history WHERE host_id=?",
                    (hid,),
                ).fetchone()
            finally:
                conn.close()
        return int(row["n"] or 0) if row else 0

    def set_host_readdress(
        self,
        host_id: str,
        address: str,
        previous_ips: List[Dict[str, str]],
        undo: Any = None,
        allow_clear_address: bool = False,
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """只改展示地址和历史 IP。host_id 与历史上报不动。

        ``undo`` 为 dict 时记下可撤销状态；``None`` 表示清掉记录。
        ``allow_clear_address`` 让撤销可以把手工地址写成空。
        """
        hid = str(host_id or "").strip()
        ip = normalize_ip(address)
        if not hid:
            return "host_id 不能为空", None
        if not ip and not allow_clear_address:
            return "新 IP 无效", None
        prev = parse_previous_ips(previous_ips)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT host_id, asset_meta FROM hosts WHERE host_id=?", (hid,)
                ).fetchone()
                if not row:
                    return "主机不存在", None
                asset = parse_asset_meta(row["asset_meta"])
                asset["address"] = ip or ""
                asset["previous_ips"] = prev
                asset["readdress_undo"] = undo
                conn.execute(
                    "UPDATE hosts SET asset_meta=? WHERE host_id=?",
                    (dump_asset_meta(asset), hid),
                )
                conn.commit()
            finally:
                conn.close()
        return None, asset

    def apply_asset_patches(
        self, patches: List[Tuple[str, Dict[str, str]]]
    ) -> Dict[str, Any]:
        """按顺序合并。同一 host_id 后面的补丁覆盖前面的。不新建主机。"""
        updated: List[str] = []
        missing: List[str] = []
        with self._lock:
            conn = self._connect()
            try:
                for host_id, patch in patches:
                    hid = str(host_id or "").strip()
                    if not hid or not patch:
                        continue
                    row = conn.execute(
                        "SELECT asset_meta FROM hosts WHERE host_id=?", (hid,)
                    ).fetchone()
                    if not row:
                        missing.append(hid)
                        continue
                    asset = parse_asset_meta(row["asset_meta"])
                    for key, value in patch.items():
                        if key in ASSET_FIELDS and str(value or "").strip():
                            asset[key] = str(value).strip()
                    conn.execute(
                        "UPDATE hosts SET asset_meta=? WHERE host_id=?",
                        (dump_asset_meta(asset), hid),
                    )
                    updated.append(hid)
                conn.commit()
            finally:
                conn.close()
        return {"updated": updated, "missing": missing}

    def set_host_hidden(
        self, host_id: str, hidden: bool
    ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        hid = str(host_id or "").strip()
        if not hid:
            return "host_id 不能为空", None
        now = int(time.time())
        flag = 1 if hidden else 0
        hidden_at: Optional[int] = now if hidden else None
        with self._lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE hosts SET hidden=?, hidden_at=? WHERE host_id=?",
                    (flag, hidden_at, hid),
                )
                conn.commit()
                if cur.rowcount <= 0:
                    return "主机不存在", None
            finally:
                conn.close()
        return None, {"host_id": hid, "hidden": bool(hidden), "hidden_at": hidden_at}

    def purge_host(self, host_id: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
        """删主机行、历史上报和分组关系。调用方必须先确认。"""
        hid = str(host_id or "").strip()
        if not hid:
            return "host_id 不能为空", None
        with self._lock:
            conn = self._connect()
            try:
                exists = conn.execute(
                    "SELECT host_id FROM hosts WHERE host_id=?", (hid,)
                ).fetchone()
                if not exists:
                    return "主机不存在", None
                hist = conn.execute(
                    "DELETE FROM metrics_history WHERE host_id=?", (hid,)
                )
                conn.execute(
                    "DELETE FROM host_group_members WHERE host_id=?", (hid,)
                )
                conn.execute("DELETE FROM hosts WHERE host_id=?", (hid,))
                conn.commit()
                history_rows = int(hist.rowcount or 0)
            finally:
                conn.close()
        return None, {"host_id": hid, "history_rows": history_rows}

    def cluster_stats(self) -> Dict[str, Any]:
        hosts = self.list_hosts()
        online = [h for h in hosts if h["online"]]
        npu_hosts = [h for h in hosts if h.get("host_type") == "npu" or (h.get("npu_count") or 0) > 0]
        gpu_hosts = [h for h in hosts if h.get("host_type") == "gpu" or (h.get("gpu_count") or 0) > 0]

        npu_cards = sum(int(h.get("npu_count") or 0) for h in hosts)
        gpu_cards = sum(int(h.get("gpu_count") or 0) for h in hosts)
        accel_cards = sum(int(h.get("accel_count") or 0) for h in hosts)
        if not accel_cards:
            accel_cards = npu_cards + gpu_cards
        cpu_cores = sum(int(h.get("cpu_count") or 0) for h in online)
        cluster_identity = merge_inventories(h.get("accel_inventory") for h in hosts)

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
        avg_accel = _avg(
            [float(h["accel_util_avg"]) for h in online if h.get("accel_util_avg") is not None]
        )
        if avg_accel is None:
            avg_accel = avg_npu if avg_npu is not None else avg_gpu

        return {
            "host_total": len(hosts),
            "host_online": len(online),
            "cpu_cores_online": cpu_cores,
            "npu_hosts": len(npu_hosts),
            "npu_cards": npu_cards,
            "gpu_hosts": len(gpu_hosts),
            "gpu_cards": gpu_cards,
            "accel_cards": accel_cards,
            "accel_summary": cluster_identity.get("summary") or None,
            "accel_inventory": cluster_identity.get("inventory") or [],
            "avg_cpu_percent": avg_cpu,
            "avg_mem_percent": avg_mem,
            "avg_npu_util_percent": avg_npu,
            "avg_gpu_util_percent": avg_gpu,
            "avg_accel_util_percent": avg_accel,
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
        excluded: List[Dict[str, Any]] = []
        for n in npus if isinstance(npus, list) else []:
            if not isinstance(n, dict):
                continue
            num, bad = classify_util_percent(n.get("util_percent"))
            if bad:
                excluded.append(
                    {"metric": "accel_util_avg", "raw": n.get("util_percent")}
                )
            elif num is not None:
                npu_utils.append(num)
            if n.get("temp_c") is not None:
                try:
                    npu_temps.append(float(n["temp_c"]))
                except (TypeError, ValueError):
                    pass
        gpu_utils: List[float] = []
        gpu_temps: List[float] = []
        for g in gpus if isinstance(gpus, list) else []:
            if not isinstance(g, dict):
                continue
            num, bad = classify_util_percent(g.get("util_percent"))
            if bad:
                excluded.append(
                    {"metric": "accel_util_avg", "raw": g.get("util_percent")}
                )
            elif num is not None:
                gpu_utils.append(num)
            if g.get("temp_c") is not None:
                try:
                    gpu_temps.append(float(g["temp_c"]))
                except (TypeError, ValueError):
                    pass
        accel_vals = npu_utils + gpu_utils
        accel_temps = npu_temps + gpu_temps
        rated = system.get("net_rated_mbps")
        rx_pct = system.get("net_rx_percent")
        tx_pct = system.get("net_tx_percent")
        if rx_pct is None:
            rx_pct = _ratio_percent(system.get("net_rx_mbps"), rated)
        if tx_pct is None:
            tx_pct = _ratio_percent(system.get("net_tx_mbps"), rated)
        def _take_percent(metric: str, raw: Any) -> Optional[float]:
            num, bad = classify_util_percent(raw)
            if bad:
                excluded.append({"metric": metric, "raw": raw})
            return num

        return {
            "ts": ts,
            "cpu_percent": _take_percent("cpu_percent", system.get("cpu_percent")),
            "mem_percent": _take_percent("mem_percent", system.get("mem_percent")),
            "disk_percent": _take_percent("disk_percent", system.get("disk_percent")),
            "excluded_utils": excluded,
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
                round(sum(accel_vals) / len(accel_vals), 2) if accel_vals else None
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
                pt.pop("excluded_utils", None)
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
    ) -> Tuple[Dict[str, List[float]], Optional[int], Optional[int], int, Dict[str, Any]]:
        series: Dict[str, List[float]] = {key: [] for key in PERIOD_METRIC_KEYS}
        first_ts = None
        last_ts = None
        parsed = 0
        excluded_count = 0
        excluded_by_metric: Dict[str, int] = {}
        for row in rows:
            pt = self._history_point_from_row(row["ts"], row["payload"])
            if not pt:
                continue
            pt = self._apply_rated_fallback(pt, rated_fallback)
            for item in pt.pop("excluded_utils", None) or []:
                if not isinstance(item, dict):
                    continue
                metric = str(item.get("metric") or "util")
                excluded_by_metric[metric] = excluded_by_metric.get(metric, 0) + 1
                excluded_count += 1
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
        return series, first_ts, last_ts, parsed, {
            "count": excluded_count,
            "by_metric": excluded_by_metric,
        }

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
        max_rows: int = PERIOD_MAX_ROWS,
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
        row_cap = max(1, int(max_rows))
        rows = self._fetch_history_rows(
            host_id, int(window["from_ts"]), int(window["to_ts"]), max_rows=row_cap
        )
        series, first_ts, last_ts, parsed, excluded = self._series_from_rows(
            rows, rated_fallback=rated_fallback
        )
        if busy_thresholds is None:
            thresholds = dict(DEFAULT_BUSY_THRESHOLDS)
        else:
            thresholds = dict(busy_thresholds)
        hostname = None
        host_type = None
        accel_summary = None
        accel_count = 0
        accel_inventory: List[Dict[str, Any]] = []
        if host is None:
            host = self.get_host(host_id)
        if host:
            hostname = host.get("hostname")
            host_type = host.get("host_type")
            payload = host.get("payload") or {}
            identity = summarize_accelerators(cards_from_payload(payload))
            accel_summary = identity.get("summary") or None
            accel_count = identity.get("count") or 0
            accel_inventory = identity.get("inventory") or []
            if not accel_count:
                accel_count = int(host.get("accel_count") or 0)
                accel_summary = host.get("accel_summary") or accel_summary
                accel_inventory = host.get("accel_inventory") or accel_inventory
        result = {
            "host_id": host_id,
            "hostname": hostname,
            "host_type": host_type,
            "accel_count": accel_count,
            "accel_summary": accel_summary,
            "accel_inventory": accel_inventory,
            "minutes": window["minutes"],
            "sample_count": parsed,
            "from_ts": first_ts,
            "to_ts": last_ts,
            "window": window,
            "busy_thresholds": thresholds,
            "metrics": self._metrics_from_series(series, thresholds),
            "history_truncated": len(rows) >= row_cap,
            "excluded_samples": excluded,
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
        max_rows: int = PERIOD_MAX_ROWS,
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
        history_truncated = False
        excluded_hosts: List[Dict[str, Any]] = []
        excluded_count = 0
        excluded_by_metric: Dict[str, int] = {}
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
                max_rows=max_rows,
            )
            series = stats.pop("_series", {}) or {}
            for key in PERIOD_METRIC_KEYS:
                pooled[key].extend(series.get(key) or [])
            total_samples += int(stats.get("sample_count") or 0)
            if stats.get("sample_count"):
                hosts_with_samples += 1
            if stats.get("history_truncated"):
                history_truncated = True
            host_excluded = stats.get("excluded_samples") or {}
            host_excluded_n = int(host_excluded.get("count") or 0)
            if host_excluded_n:
                excluded_count += host_excluded_n
                for metric, metric_n in (host_excluded.get("by_metric") or {}).items():
                    excluded_by_metric[str(metric)] = excluded_by_metric.get(
                        str(metric), 0
                    ) + int(metric_n or 0)
                excluded_hosts.append(
                    {
                        "host_id": hid,
                        "address": host.get("address") or "",
                        "hostname": host.get("hostname") or hid,
                        "count": host_excluded_n,
                        "by_metric": host_excluded.get("by_metric") or {},
                    }
                )
            host_rows.append(
                {
                    "host_id": hid,
                    "hostname": host.get("hostname") or hid,
                    "host_type": host.get("host_type"),
                    "online": host.get("online"),
                    "last_seen": host.get("last_seen"),
                    "address": host.get("address") or "",
                    "group_id": host.get("group_id"),
                    "group_name": host.get("group_name"),
                    "owner": host.get("owner") or "",
                    "purpose": host.get("purpose") or "",
                    "accel_count": host.get("accel_count") or 0,
                    "accel_summary": host.get("accel_summary") or "",
                    "accel_inventory": host.get("accel_inventory") or [],
                    "sample_count": stats.get("sample_count") or 0,
                    "from_ts": stats.get("from_ts"),
                    "to_ts": stats.get("to_ts"),
                    "metrics": stats.get("metrics") or {},
                    "history_truncated": bool(stats.get("history_truncated")),
                    "excluded_samples": host_excluded,
                }
            )

        cluster_identity = merge_inventories(
            h.get("accel_inventory") for h in host_rows
        )
        excluded_hosts.sort(key=lambda item: (-int(item.get("count") or 0), str(item.get("host_id") or "")))

        return {
            "minutes": window["minutes"],
            "window": window,
            "rollup": "sample_weighted",
            "rollup_note": "集群 avg/p95/busy_ratio 按全部主机样本合并后计算（样本多的主机权重大）；min/max 取全局最低/最高。",
            "busy_thresholds": thresholds,
            "host_count": len(host_rows),
            "hosts_with_samples": hosts_with_samples,
            "sample_count": total_samples,
            "history_truncated": history_truncated,
            "excluded_samples": {
                "count": excluded_count,
                "by_metric": excluded_by_metric,
                "hosts": excluded_hosts,
            },
            "from_ts": window["from_ts"],
            "to_ts": window["to_ts"],
            "accel_count": cluster_identity.get("count") or 0,
            "accel_summary": cluster_identity.get("summary") or None,
            "accel_inventory": cluster_identity.get("inventory") or [],
            "cluster": {
                "sample_count": total_samples,
                "metrics": self._metrics_from_series(pooled, thresholds),
                "accel_count": cluster_identity.get("count") or 0,
                "accel_summary": cluster_identity.get("summary") or None,
                "accel_inventory": cluster_identity.get("inventory") or [],
            },
            "hosts": host_rows,
        }

    def fullest_disk_mount(
        self,
        host_id: str,
        from_ts: int,
        to_ts: int,
        max_rows: int = PERIOD_MAX_ROWS,
    ) -> Optional[Dict[str, Any]]:
        """窗口内历史点里利用率最高的挂载。没有 disks[] 时返回 None，不编造。"""
        rows = self._fetch_history_rows(
            host_id, int(from_ts), int(to_ts), max_rows=max(1, int(max_rows))
        )
        best: Optional[Dict[str, Any]] = None
        samples_with_disks = 0
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            system = payload.get("system") or {}
            disks = system.get("disks")
            if not isinstance(disks, list) or not disks:
                continue
            samples_with_disks += 1
            ts = int(row["ts"])
            for item in disks:
                if not isinstance(item, dict):
                    continue
                pct, _bad = classify_util_percent(item.get("percent"))
                if pct is None:
                    continue
                candidate = {
                    "mount": item.get("mount") or "",
                    "percent": pct,
                    "used_gb": _to_float(item.get("used_gb")),
                    "total_gb": _to_float(item.get("total_gb")),
                    "ts": ts,
                    "samples_with_disks": 0,
                }
                if best is None or pct > float(best["percent"]) or (
                    pct == float(best["percent"]) and ts >= int(best["ts"])
                ):
                    best = candidate
        if best is None:
            return None
        best["samples_with_disks"] = samples_with_disks
        return best

    def save_diagnostic_report(
        self,
        period_type: str,
        title: str,
        requested_from_ts: int,
        requested_to_ts: int,
        clamped: bool,
        markdown: str,
        payload: Dict[str, Any],
        source: str = "manual",
        status: str = "ok",
        error: Optional[str] = None,
        generated_at: Optional[int] = None,
        replace: bool = True,
    ) -> Dict[str, Any]:
        """同一 (period, 半开窗口) 只保留一行。

        replace=True：覆盖最新一行，并删掉该窗口更早的重复行（id 保持为最新那条）。
        replace=False：已有则跳过，不改内容。调度器用来避免补写或覆盖手动报告。
        """
        if source not in ("manual", "scheduled"):
            source = "manual"
        if status not in ("ok", "failed"):
            status = "ok"
        gen_col = int(generated_at if generated_at is not None else time.time())
        raw = json.dumps(payload, ensure_ascii=False)
        window_key = (str(period_type), int(requested_from_ts), int(requested_to_ts))
        action = "created"
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT id FROM diagnostic_reports
                    WHERE period_type=? AND requested_from_ts=? AND requested_to_ts=?
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    window_key,
                ).fetchone()
                if row and not replace:
                    report_id = int(row["id"])
                    action = "skipped"
                elif row:
                    report_id = int(row["id"])
                    conn.execute(
                        """
                        UPDATE diagnostic_reports SET
                            title=?, clamped=?, markdown=?, payload=?,
                            source=?, status=?, error=?, generated_at=?
                        WHERE id=?
                        """,
                        (
                            title,
                            1 if clamped else 0,
                            markdown,
                            raw,
                            source,
                            status,
                            error,
                            gen_col,
                            report_id,
                        ),
                    )
                    conn.execute(
                        """
                        DELETE FROM diagnostic_reports
                        WHERE period_type=? AND requested_from_ts=? AND requested_to_ts=?
                          AND id<>?
                        """,
                        (window_key[0], window_key[1], window_key[2], report_id),
                    )
                    conn.commit()
                    action = "replaced"
                else:
                    cur = conn.execute(
                        """
                        INSERT INTO diagnostic_reports (
                            period_type, title, requested_from_ts, requested_to_ts,
                            clamped, created_at, markdown, payload,
                            source, status, error, generated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            window_key[0],
                            title,
                            window_key[1],
                            window_key[2],
                            1 if clamped else 0,
                            gen_col,
                            markdown,
                            raw,
                            source,
                            status,
                            error,
                            gen_col,
                        ),
                    )
                    conn.commit()
                    report_id = int(cur.lastrowid)
                    action = "created"
            finally:
                conn.close()
        saved = self.get_diagnostic_report(report_id) or {"id": report_id}
        saved["action"] = action
        return saved

    def list_diagnostic_reports(self, limit: int = 50) -> List[Dict[str, Any]]:
        cap = max(1, min(int(limit or 50), 200))
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    """
                    SELECT id, period_type, title, requested_from_ts, requested_to_ts,
                           clamped, created_at, generated_at, source, status, error
                    FROM diagnostic_reports
                    WHERE id IN (
                        SELECT MAX(id) FROM diagnostic_reports
                        GROUP BY period_type, requested_from_ts, requested_to_ts
                    )
                    ORDER BY COALESCE(generated_at, created_at) DESC, id DESC
                    LIMIT ?
                    """,
                    (cap,),
                ).fetchall()
            finally:
                conn.close()
        return [self._report_list_item(row) for row in rows]

    @staticmethod
    def _report_list_item(row: sqlite3.Row) -> Dict[str, Any]:
        generated = row["generated_at"]
        return {
            "id": int(row["id"]),
            "period_type": row["period_type"],
            "title": row["title"],
            "requested_from_ts": int(row["requested_from_ts"]),
            "requested_to_ts": int(row["requested_to_ts"]),
            "clamped": bool(row["clamped"]),
            "created_at": int(row["created_at"]),
            "generated_at": int(generated if generated is not None else row["created_at"]),
            "source": row["source"] or "manual",
            "status": row["status"] or "ok",
            "error": row["error"] or None,
        }

    def get_diagnostic_report(self, report_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT id, period_type, title, requested_from_ts, requested_to_ts,
                           clamped, created_at, generated_at, source, status, error,
                           markdown, payload
                    FROM diagnostic_reports WHERE id=?
                    """,
                    (int(report_id),),
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, json.JSONDecodeError):
            payload = {}
        item = self._report_list_item(row)
        item["markdown"] = row["markdown"] or ""
        item["payload"] = payload
        return item

    def export_hosts_rows(self) -> List[Dict[str, Any]]:
        return self.list_hosts()

    def cleanup_old_metrics(self) -> int:
        """只删除 metrics_history 中超期的原始点。

        diagnostic_reports 不在此清理范围内，retention_days 不会删已生成的诊断报告。
        """
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
