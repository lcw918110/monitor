"""中心端 API 业务逻辑。"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs

from center.anomaly import evaluate_cluster, judge_host_payload
from center.storage import Storage


REQUIRED_FIELDS = ("host_id", "system")


def _bad_request(msg: str) -> Tuple[int, Dict[str, Any]]:
    return 400, {"ok": False, "error": msg}


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
) -> Tuple[int, Dict[str, Any]]:
    if not storage.get_host(host_id):
        return 404, {"ok": False, "error": "主机不存在"}
    qs = parse_qs(query)
    minutes = int((qs.get("minutes") or ["60"])[0])
    limit = int((qs.get("limit") or ["240"])[0])
    points = storage.host_history(host_id, minutes=minutes, limit=limit)
    return 200, {
        "host_id": host_id,
        "minutes": minutes,
        "count": len(points),
        "points": points,
    }


def handle_host_period_stats(
    storage: Storage,
    host_id: str,
    query: str = "",
) -> Tuple[int, Dict[str, Any]]:
    if not storage.get_host(host_id):
        return 404, {"ok": False, "error": "主机不存在"}
    qs = parse_qs(query)
    minutes = int((qs.get("minutes") or ["120"])[0])
    stats = storage.host_period_stats(host_id, minutes=minutes)
    return 200, {"ok": True, **stats}


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
