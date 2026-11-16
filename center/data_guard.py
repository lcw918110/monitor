"""中心侧重点数据守护：策略、上报时重算、设置接口。

采集端可以带上观察结果。中心用当前策略再算一遍，所以白名单和阈值改完就按新的来。
未标记的主机即使带了这段，也不会入库，也不会进今日关注。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple

from common.data_guard import (
    LABEL_FEATURE,
    LIMITATION,
    evaluate,
    inventory_public,
    normalize_policy,
    public_policy,
    sanitize,
)
from center.storage import Storage


_RECOVER_MAX = 50
_RECOVER_AGE = 2 * 3600


def apply_ingest(storage: Storage, payload: Dict[str, Any]) -> Dict[str, Any]:
    """按当前标记重算事件，并改写 payload 里留下的摘要。返回给采集端的策略。"""
    host_id = str(payload.get("host_id") or "")
    policy = storage.effective_data_guard(host_id)
    raw = payload.get("data_guard")
    raw = sanitize(raw) if isinstance(raw, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    if not policy or not policy.get("enabled"):
        payload.pop("data_guard", None)
        return {"enabled": False}

    ts = int(payload.get("timestamp") or time.time())
    observation = raw.get("observation") if isinstance(raw.get("observation"), dict) else {}
    events = evaluate(policy, observation, ts=ts)
    storage.store_data_guard_events(host_id, events, replace_ts=ts)

    now = int(time.time())
    recovered_events: List[Dict[str, Any]] = []
    recovered = raw.get("recovered") if isinstance(raw.get("recovered"), list) else []
    for item in recovered[:_RECOVER_MAX]:
        if not isinstance(item, dict):
            continue
        try:
            item_ts = int(item.get("ts"))
        except (TypeError, ValueError):
            continue
        if item_ts > now + 300 or now - item_ts > _RECOVER_AGE:
            continue
        obs = item.get("observation") if isinstance(item.get("observation"), dict) else {}
        recovered_events.extend(evaluate(policy, obs, ts=item_ts))
    if recovered_events:
        storage.store_data_guard_events(host_id, recovered_events, replace_ts=None)

    payload["data_guard"] = {
        "marked": True,
        "events": events,
        "inventory": inventory_public(observation.get("paths")),
    }
    reply = public_policy(policy)
    reply["source"] = policy.get("source") or ""
    return reply


def handle_data_guard_get(storage: Storage) -> Tuple[int, Dict[str, Any]]:
    policies = storage.list_data_guard_policies()
    hosts = []
    for host in storage.list_hosts(include_hidden=True):
        guard = host.get("data_guard") or {}
        hosts.append(
            {
                "host_id": host.get("host_id"),
                "hostname": host.get("hostname") or "",
                "address": host.get("address") or "",
                "group_id": host.get("group_id"),
                "group_name": host.get("group_name") or "",
                "hidden": bool(host.get("hidden")),
                "marked": bool(guard.get("marked")),
                "source": guard.get("source") or "",
            }
        )
    enabled_groups = {
        str(item.get("scope_id"))
        for item in policies
        if item.get("scope") == "group" and item.get("enabled")
    }
    groups = []
    for group in storage.list_groups():
        gid = str(group.get("id"))
        groups.append(
            {
                "id": group.get("id"),
                "name": group.get("name") or "",
                "host_count": group.get("host_count") or 0,
                "marked": gid in enabled_groups,
            }
        )
    return 200, {
        "ok": True,
        "feature": LABEL_FEATURE,
        "limitation": LIMITATION,
        "policies": policies,
        "hosts": hosts,
        "groups": groups,
    }


def _scope(data: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    scope = str(data.get("scope") or "").strip()
    if scope not in ("host", "group"):
        return None, None, "范围只能是单台主机或一个资源组"
    scope_id = str(data.get("scope_id") or "").strip()
    if not scope_id or len(scope_id) > 128 or ".." in scope_id:
        return None, None, "请选择一台主机或一个资源组"
    if scope == "group":
        try:
            scope_id = str(int(scope_id))
        except ValueError:
            return None, None, "资源组不存在"
    return scope, scope_id, None


def handle_data_guard_write(storage: Storage, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "请求体必须是合法 JSON"}
    if not isinstance(data, dict):
        return 400, {"ok": False, "error": "请求体必须是对象"}
    scope, scope_id, err = _scope(data)
    if err or scope is None or scope_id is None:
        return 400, {"ok": False, "error": err or "范围无效"}
    if scope == "group":
        groups = {str(item.get("id")): item for item in storage.list_groups()}
        if scope_id not in groups:
            return 400, {"ok": False, "error": "资源组不存在"}
    action = str(data.get("action") or "save").strip()
    if action == "clear":
        storage.delete_data_guard_policy(scope, scope_id)
        return 200, {"ok": True}
    policy, policy_err = normalize_policy(data)
    if policy_err or policy is None:
        return 400, {"ok": False, "error": policy_err or "配置无效"}
    storage.save_data_guard_policy(scope, scope_id, policy)
    saved = dict(policy)
    saved["scope"] = scope
    saved["scope_id"] = scope_id
    return 200, {"ok": True, "policy": saved}
