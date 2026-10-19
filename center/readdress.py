"""同机更换展示 IP。

监控 host_id 和 metrics_history 保持原样。旧 IP 记进资产 JSON 的
previous_ips。新 IP 若已有部署目标，把它的 host_id 改到这台主机；
旧 IP 上、属于这台主机的目标标为停用，不删除。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from center.hostaddr import normalize_ip
from center.hostmeta import parse_previous_ips, parse_readdress_undo, target_matches_host


SHANGHAI = timezone(timedelta(hours=8))


def shanghai_today(now: Optional[datetime] = None) -> str:
    moment = now or datetime.now(SHANGHAI)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=SHANGHAI)
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d")


def next_previous_ips(
    existing: Any,
    old_ip: str,
    new_ip: str,
    date: str,
    note: str,
) -> List[Dict[str, str]]:
    """新地址从历史里拿掉；旧展示地址若不在历史里就追加一条。"""
    prev = [item for item in parse_previous_ips(existing) if item["ip"] != new_ip]
    old = normalize_ip(old_ip)
    if old and old != new_ip and all(item["ip"] != old for item in prev):
        prev.append(
            {
                "ip": old,
                "date": str(date or "")[:32],
                "note": str(note or "").strip()[:200],
            }
        )
    return prev


def plan_readdress(
    host: Dict[str, Any],
    targets: Sequence[Dict[str, Any]],
    new_ip: Any,
    note: Any = "",
    *,
    today: str = "",
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """只计算会改什么，不写库。"""
    ip = normalize_ip(new_ip)
    if not ip:
        return "新 IP 无效", None
    text = str(note or "").strip()
    if len(text) > 200:
        return "note 不能超过 200 个字符", None
    hid = str(host.get("host_id") or "").strip()
    if not hid:
        return "host_id 不能为空", None
    old_ip = normalize_ip(host.get("address"))
    if old_ip and old_ip == ip:
        return "新 IP 与当前展示地址相同", None

    new_rows = [
        target
        for target in targets
        if normalize_ip(target.get("ip")) == ip
    ]
    if len(new_rows) > 1:
        ids = ", ".join("#%s" % target.get("id") for target in new_rows)
        return "有多个部署目标使用该 IP（%s）" % ids, None

    retire: List[Dict[str, Any]] = []
    new_id = int(new_rows[0]["id"]) if new_rows else None
    for target in targets:
        try:
            tid = int(target.get("id"))
        except (TypeError, ValueError):
            continue
        if new_id is not None and tid == new_id:
            continue
        if target.get("retired"):
            continue
        tip = normalize_ip(target.get("ip"))
        if tip == ip:
            continue
        thid = str(target.get("host_id") or "").strip()
        same_host = bool(thid and thid == hid)
        old_display = bool(old_ip and tip == old_ip and target_matches_host(host, target))
        if same_host or old_display:
            retire.append(target)

    asset = host.get("asset") if isinstance(host.get("asset"), dict) else {}
    date = today or shanghai_today()
    previous = next_previous_ips(
        asset.get("previous_ips") or host.get("previous_ips"),
        old_ip,
        ip,
        date,
        text,
    )
    retargeted: List[Dict[str, Any]] = []
    if new_rows:
        row = new_rows[0]
        retargeted.append(
            {
                "id": int(row["id"]),
                "ip": ip,
                "host_id_from": str(row.get("host_id") or ""),
                "host_id_to": hid,
                "was_retired": bool(row.get("retired")),
            }
        )
    retired = [
        {
            "id": int(target["id"]),
            "ip": normalize_ip(target.get("ip")) or str(target.get("ip") or ""),
            "host_id": str(target.get("host_id") or ""),
        }
        for target in retire
    ]
    return None, {
        "host_id": hid,
        "old_ip": old_ip,
        "new_ip": ip,
        "note": text,
        "date": date,
        "address": ip,
        "previous_ips": previous,
        "deploy_targets_retargeted": retargeted,
        "deploy_targets_retired": retired,
        "host_id_unchanged": True,
        "history_rewritten": False,
    }


def readdress_undo_record(
    host: Dict[str, Any], plan: Dict[str, Any]
) -> Dict[str, Any]:
    """apply 前的目标 host_id / retired，以及这次新追加的那条历史 IP。"""
    asset = host.get("asset") if isinstance(host.get("asset"), dict) else {}
    before = {
        item["ip"]
        for item in parse_previous_ips(
            asset.get("previous_ips") or host.get("previous_ips")
        )
    }
    old = normalize_ip(plan.get("old_ip")) or ""
    after = {item["ip"] for item in parse_previous_ips(plan.get("previous_ips"))}
    added = old if old and old not in before and old in after else ""
    targets: List[Dict[str, Any]] = []
    for item in plan.get("deploy_targets_retargeted") or []:
        targets.append(
            {
                "id": int(item["id"]),
                "host_id": str(item.get("host_id_from") or ""),
                "retired": bool(item.get("was_retired")),
                "ip": item.get("ip") or "",
            }
        )
    for item in plan.get("deploy_targets_retired") or []:
        targets.append(
            {
                "id": int(item["id"]),
                "host_id": str(item.get("host_id") or ""),
                "retired": False,
                "ip": item.get("ip") or "",
            }
        )
    return {
        "added_previous_ip": added,
        "new_ip": plan.get("new_ip") or "",
        "old_ip": old,
        "note": plan.get("note") or "",
        "targets": targets,
    }


def plan_readdress_undo(
    host: Dict[str, Any],
    targets: Sequence[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """只计算如何撤销上一次改址。记录里的目标不在了就失败，不写库。"""
    asset = host.get("asset") if isinstance(host.get("asset"), dict) else {}
    record = parse_readdress_undo(
        asset.get("readdress_undo")
        if "readdress_undo" in asset
        else host.get("readdress_undo")
    )
    if not record:
        return "没有可撤销的改址记录", None
    by_id: Dict[int, Dict[str, Any]] = {}
    for target in targets:
        try:
            by_id[int(target.get("id"))] = target
        except (TypeError, ValueError):
            continue
    missing: List[int] = []
    restores: List[Dict[str, Any]] = []
    for item in record["targets"]:
        row = by_id.get(int(item["id"]))
        if row is None:
            missing.append(int(item["id"]))
            continue
        restores.append(
            {
                "id": int(item["id"]),
                "ip": normalize_ip(row.get("ip")) or item.get("ip") or "",
                "host_id_from": str(row.get("host_id") or ""),
                "host_id_to": str(item.get("host_id") or ""),
                "retired_from": bool(row.get("retired")),
                "retired_to": bool(item.get("retired")),
            }
        )
    if missing:
        ids = ", ".join("#%s" % tid for tid in missing)
        return "撤销记录里的部署目标已不存在（%s）" % ids, None
    prev = [
        item
        for item in parse_previous_ips(
            asset.get("previous_ips") or host.get("previous_ips")
        )
        if item["ip"] != record["added_previous_ip"] or not record["added_previous_ip"]
    ]
    return None, {
        "host_id": str(host.get("host_id") or ""),
        "clear_address": True,
        "remove_previous_ip": record["added_previous_ip"],
        "previous_ips": prev,
        "targets": restores,
        "added_previous_ip": record["added_previous_ip"],
        "old_ip": record["old_ip"],
        "new_ip": record["new_ip"],
        "note": record["note"],
    }
