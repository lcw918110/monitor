"""中心端 API 业务逻辑。"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from center.anomaly import evaluate_cluster, judge_host_payload
from center.storage import DEFAULT_BUSY_THRESHOLDS, PERIOD_METRIC_KEYS, Storage


REQUIRED_FIELDS = ("host_id", "system")

BUSY_QUERY_KEYS = {
    "busy_cpu": "cpu_percent",
    "busy_accel": "accel_util_avg",
    "busy_mem": "mem_percent",
    "busy_disk": "disk_percent",
}

PERIOD_CSV_FIELDS = [
    "scope",
    "host_id",
    "hostname",
    "host_type",
    "sample_count",
    "from_ts",
    "to_ts",
]
for _metric in PERIOD_METRIC_KEYS:
    PERIOD_CSV_FIELDS.extend(
        [
            "%s_avg" % _metric,
            "%s_min" % _metric,
            "%s_max" % _metric,
            "%s_p95" % _metric,
            "%s_busy_ratio" % _metric,
        ]
    )


def _bad_request(msg: str) -> Tuple[int, Dict[str, Any]]:
    return 400, {"ok": False, "error": msg}


def _parse_int(raw: Optional[str], field: str) -> Tuple[Optional[int], Optional[str]]:
    if raw is None or raw == "":
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "%s 必须是整数" % field


def _parse_float(raw: Optional[str], field: str) -> Tuple[Optional[float], Optional[str]]:
    if raw is None or raw == "":
        return None, None
    try:
        return float(raw), None
    except (TypeError, ValueError):
        return None, "%s 必须是数字" % field


def parse_busy_thresholds(
    qs: Dict[str, List[str]],
    defaults: Optional[Dict[str, float]] = None,
) -> Tuple[Optional[str], Dict[str, float]]:
    thresholds = dict(defaults or DEFAULT_BUSY_THRESHOLDS)
    for qkey, metric in BUSY_QUERY_KEYS.items():
        raw = (qs.get(qkey) or [None])[0]
        if raw is None or raw == "":
            continue
        value, err = _parse_float(raw, qkey)
        if err:
            return err, thresholds
        if value is None:
            continue
        if value < 0:
            thresholds.pop(metric, None)
        else:
            thresholds[metric] = value
    return None, thresholds


def parse_period_request(
    storage: Storage,
    query: str = "",
    defaults: Optional[Dict[str, float]] = None,
    default_minutes: int = 120,
) -> Tuple[Optional[str], Dict[str, Any], Dict[str, float], List[str]]:
    qs = parse_qs(query)
    busy_err, busy = parse_busy_thresholds(qs, defaults=defaults)
    if busy_err:
        return busy_err, {}, busy, []

    from_raw = (qs.get("from_ts") or [None])[0]
    to_raw = (qs.get("to_ts") or [None])[0]
    minutes_raw = (qs.get("minutes") or [None])[0]
    from_ts, err = _parse_int(from_raw, "from_ts")
    if err:
        return err, {}, busy, []
    to_ts, err = _parse_int(to_raw, "to_ts")
    if err:
        return err, {}, busy, []
    minutes, err = _parse_int(minutes_raw, "minutes")
    if err:
        return err, {}, busy, []
    if minutes is None and from_ts is None and to_ts is None:
        minutes = default_minutes
    win_err, window = storage.resolve_period_window(
        minutes=minutes, from_ts=from_ts, to_ts=to_ts
    )
    if win_err:
        return win_err, {}, busy, []

    host_ids_raw = (qs.get("host_ids") or [""])[0]
    host_ids = [x.strip() for x in host_ids_raw.split(",") if x.strip()] if host_ids_raw else []
    return None, window, busy, host_ids


def handle_metrics_post(
    storage: Storage,
    body: bytes,
    expected_token: str = "",
) -> Tuple[int, Dict[str, Any]]:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("请求体必须是合法 JSON")

    if not isinstance(payload, dict):
        return _bad_request("JSON 根节点必须是对象")

    for field in REQUIRED_FIELDS:
        if field not in payload:
            return _bad_request(f"缺少字段: {field}")

    if not payload.get("host_id"):
        return _bad_request("host_id 不能为空")

    if not isinstance(payload.get("system"), dict):
        return _bad_request("system 必须是对象")

    for key in ("gpus", "npus", "gpu_processes", "accelerators"):
        val = payload.get(key, [])
        if val is None:
            payload[key] = []
        elif not isinstance(val, list):
            return _bad_request("%s 必须是数组" % key)

    if expected_token:
        if payload.get("token") != expected_token:
            return 401, {"ok": False, "error": "token 无效"}

    payload = dict(payload)
    payload.pop("token", None)

    from common.host_type import normalize_host_type, resolve_auto_host_type

    host_type = normalize_host_type(payload.get("host_type"))
    if host_type == "auto":
        has_cards = bool(payload.get("accelerators") or payload.get("npus") or payload.get("gpus"))
        host_type = resolve_auto_host_type(has_cards)
    payload["host_type"] = host_type

    storage.upsert_metric(payload)
    return 200, {"ok": True}


def handle_hosts_list(
    storage: Storage,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    hosts = storage.list_hosts()
    rank = {"critical": 0, "warn": 1, "unknown": 2, "normal": 3}
    for h in hosts:
        detail = storage.get_host(h["host_id"])
        payload = (detail or {}).get("payload") or {}
        judged = judge_host_payload(
            payload, thresholds=thresholds, online=bool(h.get("online"))
        )
        h["anomaly"] = judged
    hosts.sort(
        key=lambda x: (
            0 if x.get("online") else 1,
            rank.get(((x.get("anomaly") or {}).get("overall") or "unknown"), 9),
            x.get("host_id") or "",
        )
    )
    return 200, {"hosts": hosts}


def handle_host_detail(
    storage: Storage,
    host_id: str,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    host = storage.get_host(host_id)
    if not host:
        return 404, {"ok": False, "error": "主机不存在"}
    host["anomaly"] = judge_host_payload(
        host.get("payload") or {},
        thresholds=thresholds,
        online=bool(host.get("online")),
    )
    return 200, host


def handle_host_history(
    storage: Storage,
    host_id: str,
    query: str = "",
    busy_defaults: Optional[Dict[str, float]] = None,
) -> Tuple[int, Dict[str, Any]]:
    if not storage.get_host(host_id):
        return 404, {"ok": False, "error": "主机不存在"}
    qs = parse_qs(query)
    err, window, _busy, _ids = parse_period_request(
        storage, query, defaults=busy_defaults, default_minutes=60
    )
    if err:
        return _bad_request(err)
    limit_raw = (qs.get("limit") or ["240"])[0]
    limit, limit_err = _parse_int(limit_raw, "limit")
    if limit_err:
        return _bad_request(limit_err)
    if limit is None:
        limit = 240
    points = storage.host_history(
        host_id,
        minutes=window["minutes"],
        limit=limit,
        from_ts=window["from_ts"],
        to_ts=window["to_ts"],
    )
    return 200, {
        "host_id": host_id,
        "minutes": window["minutes"],
        "window": window,
        "count": len(points),
        "points": points,
    }


def handle_host_period_stats(
    storage: Storage,
    host_id: str,
    query: str = "",
    busy_defaults: Optional[Dict[str, float]] = None,
) -> Tuple[int, Dict[str, Any]]:
    if not storage.get_host(host_id):
        return 404, {"ok": False, "error": "主机不存在"}
    err, window, busy, _ids = parse_period_request(
        storage, query, defaults=busy_defaults
    )
    if err:
        return _bad_request(err)
    stats = storage.host_period_stats(
        host_id,
        minutes=window["minutes"],
        from_ts=window["from_ts"],
        to_ts=window["to_ts"],
        busy_thresholds=busy,
        window=window,
    )
    stats.pop("ok", None)
    if stats.get("error"):
        return _bad_request(str(stats["error"]))
    return 200, {"ok": True, **stats}


def handle_cluster_period_stats(
    storage: Storage,
    query: str = "",
    busy_defaults: Optional[Dict[str, float]] = None,
) -> Tuple[int, Dict[str, Any]]:
    err, window, busy, host_ids = parse_period_request(
        storage, query, defaults=busy_defaults
    )
    if err:
        return _bad_request(err)
    stats = storage.cluster_period_stats(
        minutes=window["minutes"],
        from_ts=window["from_ts"],
        to_ts=window["to_ts"],
        host_ids=host_ids or None,
        busy_thresholds=busy,
        window=window,
    )
    if stats.get("ok") is False:
        return _bad_request(str(stats.get("error") or "时段统计失败"))
    return 200, {"ok": True, **stats}


def _flatten_period_row(
    scope: str,
    host_id: str,
    hostname: Any,
    host_type: Any,
    sample_count: Any,
    from_ts: Any,
    to_ts: Any,
    metrics: Dict[str, Any],
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "scope": scope,
        "host_id": host_id,
        "hostname": hostname or "",
        "host_type": host_type or "",
        "sample_count": sample_count or 0,
        "from_ts": from_ts or "",
        "to_ts": to_ts or "",
    }
    for metric in PERIOD_METRIC_KEYS:
        agg = (metrics or {}).get(metric) or {}
        row["%s_avg" % metric] = agg.get("avg")
        row["%s_min" % metric] = agg.get("min")
        row["%s_max" % metric] = agg.get("max")
        row["%s_p95" % metric] = agg.get("p95")
        row["%s_busy_ratio" % metric] = agg.get("busy_ratio")
    return row


def handle_export_period_csv(
    storage: Storage,
    query: str = "",
    busy_defaults: Optional[Dict[str, float]] = None,
) -> Tuple[int, Any]:
    code, payload = handle_cluster_period_stats(
        storage, query=query, busy_defaults=busy_defaults
    )
    if code != 200:
        return code, payload
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=PERIOD_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    cluster = payload.get("cluster") or {}
    window = payload.get("window") or {}
    writer.writerow(
        _flatten_period_row(
            "cluster",
            "__cluster__",
            "集群汇总",
            "",
            cluster.get("sample_count") or payload.get("sample_count"),
            window.get("from_ts"),
            window.get("to_ts"),
            cluster.get("metrics") or {},
        )
    )
    for host in payload.get("hosts") or []:
        writer.writerow(
            _flatten_period_row(
                "host",
                host.get("host_id") or "",
                host.get("hostname"),
                host.get("host_type"),
                host.get("sample_count"),
                host.get("from_ts"),
                host.get("to_ts"),
                host.get("metrics") or {},
            )
        )
    return 200, buf.getvalue()


def handle_stats(storage: Storage) -> Tuple[int, Dict[str, Any]]:
    return 200, storage.cluster_stats()


def handle_anomaly(
    storage: Storage,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    return 200, evaluate_cluster(storage, thresholds=thresholds)


def handle_export_json(storage: Storage) -> Tuple[int, Dict[str, Any]]:
    return 200, {"hosts": storage.export_hosts_rows()}


def handle_export_csv(storage: Storage) -> Tuple[int, str]:
    rows = storage.export_hosts_rows()
    buf = io.StringIO()
    fields = [
        "host_id",
        "hostname",
        "host_type",
        "online",
        "last_seen",
        "cpu_percent",
        "cpu_count",
        "mem_percent",
        "npu_count",
        "npu_util_avg",
        "gpu_count",
        "gpu_util_avg",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return 200, buf.getvalue()
