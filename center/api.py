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
    "accel_count",
    "accel_summary",
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
    remote_ip: str = "",
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

    storage.upsert_metric(payload, remote_ip=remote_ip)
    return 200, {"ok": True}


def _query_flag(qs: Dict[str, List[str]], key: str) -> bool:
    raw = (qs.get(key) or [""])[0]
    return str(raw).strip().lower() in ("1", "true", "yes")


def handle_hosts_list(
    storage: Storage,
    thresholds: Optional[Dict[str, Any]] = None,
    query: str = "",
) -> Tuple[int, Dict[str, Any]]:
    qs = parse_qs(query or "")
    hidden_only = _query_flag(qs, "hidden")
    include_hidden = _query_flag(qs, "include_hidden")
    hosts = storage.list_hosts(
        include_hidden=include_hidden, hidden_only=hidden_only
    )
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
    accel_count: Any = None,
    accel_summary: Any = None,
) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "scope": scope,
        "host_id": host_id,
        "hostname": hostname or "",
        "host_type": host_type or "",
        "accel_count": accel_count or 0,
        "accel_summary": accel_summary or "",
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
            cluster.get("accel_count") or payload.get("accel_count"),
            cluster.get("accel_summary") or payload.get("accel_summary"),
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
                host.get("accel_count"),
                host.get("accel_summary"),
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
        "address",
        "group_name",
        "host_type",
        "online",
        "last_seen",
        "cpu_percent",
        "cpu_count",
        "mem_percent",
        "disk_percent",
        "disk_used_gb",
        "disk_total_gb",
        "disk_count",
        "load1",
        "npu_count",
        "npu_util_avg",
        "gpu_count",
        "gpu_util_avg",
        "accel_count",
        "accel_summary",
        "net_rx_mbps",
        "net_tx_mbps",
        "net_rated_mbps",
        "owner",
        "purpose",
        "asset_name",
        "asset_cpu",
        "asset_memory",
        "asset_disk",
        "asset_gpu",
        "asset_model",
        "asset_notes",
    ]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        asset = row.get("asset") or {}
        flat = dict(row)
        flat["asset_name"] = asset.get("name") or ""
        flat["asset_cpu"] = asset.get("cpu") or ""
        flat["asset_memory"] = asset.get("memory") or ""
        flat["asset_disk"] = asset.get("disk") or ""
        flat["asset_gpu"] = asset.get("gpu") or ""
        flat["asset_model"] = asset.get("model") or ""
        flat["asset_notes"] = asset.get("notes") or ""
        writer.writerow(flat)
    return 200, buf.getvalue()


def handle_groups_list(storage: Storage) -> Tuple[int, Dict[str, Any]]:
    return 200, {"groups": storage.list_groups()}


def handle_group_create(
    storage: Storage, body: bytes
) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("请求体必须是合法 JSON")
    if not isinstance(data, dict):
        return _bad_request("JSON 根节点必须是对象")
    err, group = storage.create_group(data.get("name"))
    if err:
        return _bad_request(err)
    return 200, {"ok": True, "group": group}


def handle_group_rename(
    storage: Storage, group_id: int, body: bytes
) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("请求体必须是合法 JSON")
    if not isinstance(data, dict):
        return _bad_request("JSON 根节点必须是对象")
    err, group = storage.rename_group(group_id, data.get("name"))
    if err:
        code = 404 if err == "资源组不存在" else 400
        return code, {"ok": False, "error": err}
    return 200, {"ok": True, "group": group}


def handle_group_delete(storage: Storage, group_id: int) -> Tuple[int, Dict[str, Any]]:
    if not storage.delete_group(group_id):
        return 404, {"ok": False, "error": "资源组不存在"}
    return 200, {"ok": True}


def handle_host_assign_group(
    storage: Storage, host_id: str, body: bytes
) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad_request("请求体必须是合法 JSON")
    if not isinstance(data, dict):
        return _bad_request("JSON 根节点必须是对象")
    raw = data.get("group_id", None)
    group_id: Optional[int]
    if raw is None or raw == "" or raw is False:
        group_id = None
    else:
        try:
            group_id = int(raw)
        except (TypeError, ValueError):
            return _bad_request("group_id 必须是整数或空")
    err, info = storage.assign_host_group(host_id, group_id)
    if err:
        code = 404 if err in ("主机不存在", "资源组不存在") else 400
        return code, {"ok": False, "error": err}
    return 200, {"ok": True, **(info or {})}


def _parse_json_object(body: bytes) -> Tuple[Optional[str], Dict[str, Any]]:
    if not body:
        return None, {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "请求体必须是合法 JSON", {}
    if not isinstance(data, dict):
        return "JSON 根节点必须是对象", {}
    return None, data


def _linked_deploy_targets(deploy_store: Any, host: Dict[str, Any]) -> List[int]:
    if deploy_store is None or not host:
        return []
    from center.hostmeta import target_matches_host

    removed: List[int] = []
    try:
        targets = deploy_store.list_targets()
    except Exception:  # noqa: BLE001
        return []
    for target in targets:
        if not target_matches_host(host, target):
            continue
        try:
            tid = int(target.get("id"))
        except (TypeError, ValueError):
            continue
        if deploy_store.delete_target(tid):
            removed.append(tid)
    return removed


def handle_host_meta_update(
    storage: Storage, host_id: str, body: bytes
) -> Tuple[int, Dict[str, Any]]:
    from center.hostmeta import sanitize_asset_patch

    err, data = _parse_json_object(body)
    if err:
        return _bad_request(err)
    err, patch = sanitize_asset_patch(data, allow_clear=True)
    if err:
        return _bad_request(err)
    if not patch:
        return _bad_request("没有可更新的资产字段")
    err, asset = storage.update_host_asset(host_id, patch)
    if err:
        code = 404 if err == "主机不存在" else 400
        return code, {"ok": False, "error": err}
    return 200, {"ok": True, "host_id": host_id, "asset": asset}


def handle_host_hide(
    storage: Storage,
    host_id: str,
    body: bytes,
    deploy_store: Any = None,
) -> Tuple[int, Dict[str, Any]]:
    err, data = _parse_json_object(body)
    if err:
        return _bad_request(err)
    host = storage.get_host(host_id)
    if not host:
        return 404, {"ok": False, "error": "主机不存在"}
    err, info = storage.set_host_hidden(host_id, True)
    if err:
        return 404, {"ok": False, "error": err}
    removed: List[int] = []
    if data.get("remove_deploy_target"):
        removed = _linked_deploy_targets(deploy_store, host)
    return 200, {"ok": True, **(info or {}), "removed_deploy_targets": removed}


def handle_host_restore(storage: Storage, host_id: str) -> Tuple[int, Dict[str, Any]]:
    err, info = storage.set_host_hidden(host_id, False)
    if err:
        code = 404 if err == "主机不存在" else 400
        return code, {"ok": False, "error": err}
    return 200, {"ok": True, **(info or {})}


def handle_host_delete(
    storage: Storage,
    host_id: str,
    query: str = "",
    deploy_store: Any = None,
) -> Tuple[int, Dict[str, Any]]:
    qs = parse_qs(query or "")
    confirm = (qs.get("confirm") or [""])[0].strip().lower()
    if confirm != "delete":
        return 400, {
            "ok": False,
            "error": "硬删除需要查询参数 confirm=delete",
        }
    host = storage.get_host(host_id)
    if not host:
        return 404, {"ok": False, "error": "主机不存在"}
    removed: List[int] = []
    if _query_flag(qs, "remove_deploy_target"):
        removed = _linked_deploy_targets(deploy_store, host)
    err, info = storage.purge_host(host_id)
    if err:
        return 404, {"ok": False, "error": err}
    return 200, {"ok": True, **(info or {}), "removed_deploy_targets": removed}


def handle_host_meta_import(
    storage: Storage,
    body: bytes,
    query: str = "",
) -> Tuple[int, Dict[str, Any]]:
    from center.hostmeta import decode_import_body, iter_import_patches, plan_import

    qs = parse_qs(query or "")
    err, rows, body_apply = decode_import_body(body)
    if err:
        return _bad_request(err)
    apply = _query_flag(qs, "apply") or body_apply
    hosts = storage.list_hosts(include_hidden=True)
    err, plan = plan_import(hosts, rows)
    if err:
        payload = {"ok": False, "error": err}
        payload.update(plan or {})
        return 400, payload
    result: Dict[str, Any] = {
        "ok": True,
        "apply": bool(apply),
        "ignored_columns": plan.get("ignored_columns") or [],
        "matched": plan.get("matched") or [],
        "unmatched": plan.get("unmatched") or [],
        "ambiguous": plan.get("ambiguous") or [],
        "skipped": plan.get("skipped") or [],
        "summary": plan.get("summary") or {},
    }
    if apply:
        applied = storage.apply_asset_patches(list(iter_import_patches(plan)))
        result["applied"] = applied
        result["summary"] = dict(result["summary"])
        result["summary"]["updated"] = len(applied.get("updated") or [])
    return 200, result


def handle_host_readdress(
    storage: Storage,
    host_id: str,
    body: bytes,
    deploy_store: Any = None,
) -> Tuple[int, Dict[str, Any]]:
    """预览或执行同机改址。默认只预览；``apply: true`` 才写库。"""
    from center.readdress import plan_readdress

    err, data = _parse_json_object(body)
    if err:
        return _bad_request(err)
    host = storage.get_host(host_id)
    if not host:
        return 404, {"ok": False, "error": "主机不存在"}
    targets: List[Dict[str, Any]] = []
    if deploy_store is not None:
        try:
            targets = deploy_store.list_targets()
        except Exception:  # noqa: BLE001
            targets = []
    err, plan = plan_readdress(
        host,
        targets,
        data.get("new_ip"),
        data.get("note") or "",
    )
    if err or not plan:
        return _bad_request(err or "无法更换 IP")
    history_rows = storage.history_count(host_id)
    apply = data.get("apply") is True or data.get("apply") == 1
    result: Dict[str, Any] = {
        "ok": True,
        "apply": bool(apply),
        "history_rows": history_rows,
    }
    result.update(plan)
    if not apply:
        return 200, result

    retargeted = list(plan.get("deploy_targets_retargeted") or [])
    retired = list(plan.get("deploy_targets_retired") or [])
    touched: List[Tuple[str, int, str]] = []
    try:
        if deploy_store is not None:
            for item in retargeted:
                tid = int(item["id"])
                if item.get("host_id_from") != item.get("host_id_to"):
                    deploy_store.assign_target_host_id(tid, plan["host_id"])
                    touched.append(("host_id", tid, str(item.get("host_id_from") or "")))
                if item.get("was_retired"):
                    deploy_store.set_target_retired(tid, False)
                    touched.append(("unretired", tid, ""))
            for item in retired:
                tid = int(item["id"])
                deploy_store.set_target_retired(tid, True)
                touched.append(("retired", tid, ""))
        err, _asset = storage.set_host_readdress(
            host_id, plan["address"], plan["previous_ips"]
        )
        if err:
            raise ValueError(err)
    except ValueError as exc:
        _rollback_readdress(deploy_store, touched)
        text = str(exc)
        code = 404 if text == "主机不存在" else 400
        return code, {"ok": False, "error": text}

    fresh = storage.get_host(host_id) or {}
    result["history_rows"] = storage.history_count(host_id)
    result["address"] = fresh.get("address") or plan["address"]
    result["previous_ips"] = fresh.get("previous_ips") or plan["previous_ips"]
    return 200, result


def _rollback_readdress(deploy_store: Any, touched: List[Tuple[str, int, str]]) -> None:
    if deploy_store is None:
        return
    for kind, tid, previous in reversed(touched):
        try:
            if kind == "host_id":
                deploy_store.assign_target_host_id(tid, previous)
            elif kind == "retired":
                deploy_store.set_target_retired(tid, False)
            elif kind == "unretired":
                deploy_store.set_target_retired(tid, True)
        except Exception:  # noqa: BLE001
            continue
