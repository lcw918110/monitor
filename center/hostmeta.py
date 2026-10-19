"""主机手工资产信息：校验、采集规格对照、按 IP 导入。

手工字段只由页面或导入写入。Agent 上报不会改这些列。
导入若带用户名、密码一类列，整列丢弃，不进库、不进预览。
"""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from center.hostaddr import collect_payload_ips, normalize_ip, parse_host_id_ip
from common.accel_identity import cards_from_payload, summarize_accelerators


# 短字段给表格里的人名、用途；型号和备注允许更长的自由文本。
ASSET_LIMITS = {
    "owner": 120,
    "purpose": 500,
    "name": 200,
    "category": 40,
    "vm_host": 200,
    "monitor_flag": 16,
    "cpu": 2000,
    "memory": 2000,
    "disk": 4000,
    "gpu": 2000,
    "model": 4000,
    "notes": 4000,
    "address": 64,
}

ASSET_FIELDS = tuple(ASSET_LIMITS.keys())

# 规范化后的表头 → 资产字段。ip 只用来匹配，不写入资产。
_HEADER_FIELDS = {
    "负责人": "owner",
    "使用人": "owner",
    "使用人/负责人": "owner",
    "owner": "owner",
    "用途": "purpose",
    "purpose": "purpose",
    "cpu": "cpu",
    "处理器": "cpu",
    "内存": "memory",
    "memory": "memory",
    "硬盘": "disk",
    "磁盘": "disk",
    "disk": "disk",
    "gpu": "gpu",
    "加速卡": "gpu",
    "型号": "model",
    "型号/资产编号": "model",
    "资产编号": "model",
    "model": "model",
    "备注": "notes",
    "notes": "notes",
    "名称": "name",
    "name": "name",
    "分类": "category",
    "category": "category",
    "宿主机": "vm_host",
    "vm_host": "vm_host",
    "是否资源监控": "monitor_flag",
    "ip地址": "ip",
    "ip": "ip",
    "地址": "ip",
    "管理ip": "ip",
}

# 「硬盘/型号」在资产表里是一列：纯容量进硬盘，其余进型号/资产编号。
_DISK_OR_MODEL = "硬盘/型号"

_CREDENTIAL_MARKERS = (
    "用户名",
    "密码",
    "口令",
    "password",
    "passwd",
    "username",
    "secret",
    "token",
    "ssh_",
)

_SIZE_ONLY = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:tb|gb|mb|t|g)\s*$",
    re.IGNORECASE,
)

_FORBIDDEN_API_KEYS = {
    "password",
    "passwd",
    "ssh_password",
    "token",
    "secret",
    "username",
    "ssh_user",
    "用户名",
    "密码",
    "口令",
}


def empty_asset() -> Dict[str, Any]:
    out: Dict[str, Any] = {key: "" for key in ASSET_FIELDS}
    out["previous_ips"] = []
    out["readdress_undo"] = None
    return out


def parse_previous_ips(raw: Any) -> List[Dict[str, str]]:
    """历史 IP。每项 ``{"ip","date","note"}``，坏数据丢掉，同一 IP 只留第一次。"""
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            raw = [text]
    if not isinstance(raw, list):
        return []
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        note = ""
        date = ""
        if isinstance(item, dict):
            ip = normalize_ip(item.get("ip") or item.get("address"))
            note = str(item.get("note") or "").strip()[:200]
            date = str(item.get("date") or "").strip()[:32]
        else:
            ip = normalize_ip(item)
        if not ip or ip in seen:
            continue
        seen.add(ip)
        out.append({"ip": ip, "date": date, "note": note})
    return out


def parse_readdress_undo(raw: Any) -> Optional[Dict[str, Any]]:
    """改址 apply 时记下的可撤销状态。坏数据当成没有记录。"""
    if not isinstance(raw, dict):
        return None
    targets: List[Dict[str, Any]] = []
    for item in raw.get("targets") or []:
        if not isinstance(item, dict):
            continue
        try:
            tid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        targets.append(
            {
                "id": tid,
                "host_id": str(item.get("host_id") or ""),
                "retired": bool(item.get("retired")),
                "ip": normalize_ip(item.get("ip")) or str(item.get("ip") or ""),
            }
        )
    return {
        "added_previous_ip": normalize_ip(raw.get("added_previous_ip")) or "",
        "new_ip": normalize_ip(raw.get("new_ip")) or "",
        "old_ip": normalize_ip(raw.get("old_ip")) or "",
        "note": str(raw.get("note") or "").strip()[:200],
        "targets": targets,
    }


def sanitize_previous_ips_value(
    data: Any,
) -> Tuple[Optional[str], Optional[List[Dict[str, str]]]]:
    """``previous_ips`` 缺省表示不改。``[]`` 清空。无效 IP 拒绝，不静默丢掉。"""
    if not isinstance(data, dict) or "previous_ips" not in data:
        return None, None
    raw = data.get("previous_ips")
    if raw is None:
        return None, []
    if not isinstance(raw, list):
        return "previous_ips 必须是数组", None
    out: List[Dict[str, str]] = []
    seen = set()
    for item in raw:
        note = ""
        date = ""
        if isinstance(item, dict):
            ip = normalize_ip(item.get("ip") or item.get("address"))
            note = str(item.get("note") or "").strip()[:200]
            date = str(item.get("date") or "").strip()[:32]
        elif isinstance(item, str):
            ip = normalize_ip(item)
        else:
            ip = ""
        if not ip:
            return "previous_ips 含有无效 IP", None
        if ip in seen:
            continue
        seen.add(ip)
        out.append({"ip": ip, "date": date, "note": note})
    return None, out


def parse_asset_meta(raw: Any) -> Dict[str, Any]:
    """库里的 JSON。缺键、坏 JSON 都当成空，不抛给调用方。

    ``previous_ips`` 是列表，其余手工字段是字符串。两边都要读出来，
    否则下一次只改负责人会把历史 IP 写丢。
    """
    out = empty_asset()
    if raw is None or raw == "":
        return out
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return out
    if not isinstance(data, dict):
        return out
    for key in ASSET_FIELDS:
        value = data.get(key)
        if value is None:
            continue
        out[key] = str(value)
    out["previous_ips"] = parse_previous_ips(data.get("previous_ips"))
    out["readdress_undo"] = parse_readdress_undo(data.get("readdress_undo"))
    return out


def dump_asset_meta(asset: Dict[str, Any]) -> str:
    clean = empty_asset()
    clean.update({key: str(asset.get(key) or "") for key in ASSET_FIELDS})
    clean["previous_ips"] = parse_previous_ips(asset.get("previous_ips"))
    undo = parse_readdress_undo(asset.get("readdress_undo"))
    if undo:
        clean["readdress_undo"] = undo
    else:
        clean.pop("readdress_undo", None)
    return json.dumps(clean, ensure_ascii=False)


def _norm_header(text: Any) -> str:
    raw = str(text or "").strip().strip("\ufeff")
    raw = raw.replace(" ", "").replace("\u3000", "")
    return raw.lower()


def is_credential_header(text: Any) -> bool:
    norm = _norm_header(text)
    if not norm:
        return False
    return any(marker in norm for marker in _CREDENTIAL_MARKERS)


def _limit_error(key: str, value: str) -> Optional[str]:
    limit = ASSET_LIMITS[key]
    if len(value) > limit:
        return "%s 不能超过 %d 个字符" % (key, limit)
    return None


def sanitize_asset_patch(
    data: Any,
    *,
    allow_clear: bool = True,
) -> Tuple[Optional[str], Dict[str, str]]:
    """手工编辑：给出的键覆盖，空字符串清空。未给出的键不动。

    密码一类键直接拒绝，避免经由这个接口写进资产 JSON。
    """
    if not isinstance(data, dict):
        return "资产信息必须是对象", {}
    for key in data:
        if str(key) in _FORBIDDEN_API_KEYS or is_credential_header(key):
            return "资产信息不能包含凭据字段", {}
    patch: Dict[str, str] = {}
    for key in ASSET_FIELDS:
        if key not in data:
            continue
        value = data.get(key)
        if value is None:
            value = ""
        text = str(value).strip()
        if not text and not allow_clear:
            continue
        if key == "address" and text:
            ip = normalize_ip(text)
            if not ip:
                return "展示地址不是有效的 IPv4", {}
            text = ip
        err = _limit_error(key, text)
        if err:
            return err, {}
        patch[key] = text
    return None, patch


def split_disk_or_model(text: str) -> Tuple[str, str]:
    """纯容量（如 324.11 GB、500G）进硬盘；其余自由文本进型号/资产编号。"""
    raw = str(text or "").strip()
    if not raw:
        return "", ""
    compact = re.sub(r"\s+", "", raw)
    if _SIZE_ONLY.match(raw) or _SIZE_ONLY.match(compact):
        return raw, ""
    return "", raw


def collected_hardware(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Agent 已经上报的规格。只读，不写回手工字段。"""
    payload = payload or {}
    system = payload.get("system") or {}
    if not isinstance(system, dict):
        system = {}
    disks_out: List[Dict[str, Any]] = []
    raw_disks = system.get("disks")
    if isinstance(raw_disks, list):
        for item in raw_disks:
            if not isinstance(item, dict):
                continue
            disks_out.append(
                {
                    "mount": item.get("mount") or "",
                    "total_gb": item.get("total_gb"),
                    "fstype": item.get("fstype") or "",
                }
            )
    disk_count = system.get("disk_count")
    if disk_count is None and disks_out:
        disk_count = len(disks_out)
    identity = summarize_accelerators(cards_from_payload(payload))
    return {
        "cpu_model": system.get("cpu_model") or "",
        "cpu_count": system.get("cpu_count"),
        "cpu_arch": system.get("cpu_arch") or "",
        "mem_total_mb": system.get("mem_total_mb"),
        "disk_total_gb": system.get("disk_total_gb"),
        "disk_count": disk_count,
        "disks": disks_out,
        "accel_count": identity.get("count") or 0,
        "accel_summary": identity.get("summary") or "",
        "accel_inventory": identity.get("inventory") or [],
    }


def host_match_ips(host: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> List[str]:
    """一台主机可用于和资产表对齐的 IP，去重且保持稳定顺序。"""
    found: List[str] = []
    seen = set()

    def add(value: Any) -> None:
        ip = normalize_ip(value)
        if not ip or ip in seen:
            return
        seen.add(ip)
        found.append(ip)

    asset = host.get("asset") if isinstance(host.get("asset"), dict) else {}
    add(asset.get("address"))
    add(host.get("address"))
    add(parse_host_id_ip(host.get("host_id")))
    add(host.get("last_remote_ip"))

    def add_previous(raw: Any) -> None:
        if not isinstance(raw, list):
            return
        for item in raw:
            if isinstance(item, dict):
                add(item.get("ip") or item.get("address"))
            else:
                add(item)

    add_previous(asset.get("previous_ips"))
    add_previous(host.get("previous_ips"))
    body = payload if payload is not None else host.get("payload")
    if isinstance(body, dict):
        for ip in collect_payload_ips(body):
            add(ip)
    extra = host.get("ips")
    if isinstance(extra, list):
        for ip in extra:
            add(ip)
    return found


def target_matches_host(host: Dict[str, Any], target: Dict[str, Any]) -> bool:
    """部署清单和监控主机对齐。

    host_id 相同则命中。只按 IP 命中时，要求清单自己的 host_id 为空、
    等于 IP，或等于这台监控主机，避免误删另一台同网段机器的目标。
    """
    hid = str(host.get("host_id") or "").strip()
    thid = str(target.get("host_id") or "").strip()
    if hid and thid and thid == hid:
        return True
    tip = normalize_ip(target.get("ip"))
    if not tip or tip not in set(host_match_ips(host)):
        return False
    if not thid or thid == tip or thid == hid:
        return True
    # 清单 host_id 自己嵌着这个 IP（host-172-24-5-3）时，视为同一台。
    return parse_host_id_ip(thid) == tip


def decode_import_body(body: bytes) -> Tuple[Optional[str], List[List[str]], bool]:
    """把上传体解成二维表。第三个返回值表示调用方是否在 JSON 里要求 apply。

    JSON 只接受 csv 文本或 rows，不接受凭据对象。xlsx 以 PK 头识别。
    """
    if not body:
        return "空文件", [], False
    stripped = body.lstrip()
    if stripped.startswith(b"{") or stripped.startswith(b"["):
        try:
            data = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "请求体不是合法 JSON", [], False
        apply = False
        if isinstance(data, dict):
            apply = bool(data.get("apply"))
            if "rows" in data:
                rows = data.get("rows")
                if not isinstance(rows, list):
                    return "rows 必须是二维数组", [], apply
                table = [_stringify_row(row) for row in rows]
                return None, table, apply
            text = data.get("csv")
            if not isinstance(text, str):
                return "JSON 需要 csv 文本或 rows", [], apply
            return None, _parse_csv_text(text), apply
        return "JSON 根节点必须是对象", [], False
    if body.startswith(b"PK"):
        try:
            from center.xlsx_util import read_xlsx_rows

            return None, read_xlsx_rows(body), False
        except Exception as exc:  # noqa: BLE001
            return "解析 Excel 失败: %s" % exc, [], False
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("gbk", errors="replace")
    return None, _parse_csv_text(text), False


def _stringify_row(row: Any) -> List[str]:
    if isinstance(row, list):
        return ["" if cell is None else str(cell).strip() for cell in row]
    return ["" if row is None else str(row).strip()]


def _parse_csv_text(text: str) -> List[List[str]]:
    sample = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(sample))
    rows: List[List[str]] = []
    for row in reader:
        rows.append([cell.strip() for cell in row])
    return rows


def _map_headers(
    header: Sequence[str],
) -> Tuple[List[Optional[str]], List[str]]:
    """返回与表头等长的字段名列表，以及被忽略的凭据列原名。"""
    fields: List[Optional[str]] = []
    ignored: List[str] = []
    for cell in header:
        label = str(cell or "").strip()
        if not label:
            fields.append(None)
            continue
        if is_credential_header(label):
            ignored.append(label)
            fields.append(None)
            continue
        norm = _norm_header(label)
        if norm == _norm_header(_DISK_OR_MODEL):
            fields.append("disk_or_model")
            continue
        fields.append(_HEADER_FIELDS.get(norm))
    return fields, ignored


def plan_import(
    hosts: Sequence[Dict[str, Any]],
    rows: Sequence[Sequence[str]],
) -> Tuple[Optional[str], Dict[str, Any]]:
    """按 IP 预览。不写库。空单元格不覆盖已有手工信息。"""
    table = [list(row) for row in rows if any(str(cell or "").strip() for cell in row)]
    if not table:
        return "没有可导入的行", {}
    header = table[0]
    fields, ignored = _map_headers(header)
    if "ip" not in fields:
        return "需要 IP地址 列（或 ip / 地址）才能按 IP 匹配已有主机", {
            "ignored_columns": ignored,
        }

    index: Dict[str, List[Dict[str, Any]]] = {}
    for host in hosts:
        for ip in host_match_ips(host):
            index.setdefault(ip, []).append(host)

    matched: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    seen_ip_row: Dict[str, int] = {}

    for offset, row in enumerate(table[1:], start=2):
        padded = list(row) + [""] * max(0, len(fields) - len(row))
        ip_text = ""
        patch: Dict[str, str] = {}
        truncated: List[str] = []
        for idx, field in enumerate(fields):
            if not field or idx >= len(padded):
                continue
            cell = str(padded[idx] or "").strip()
            if field == "ip":
                if not ip_text and cell:
                    ip_text = cell
                continue
            if not cell:
                continue
            if field == "disk_or_model":
                disk, model = split_disk_or_model(cell)
                if disk:
                    _put_import_field(patch, truncated, "disk", disk)
                if model:
                    _put_import_field(patch, truncated, "model", model)
                continue
            _put_import_field(patch, truncated, field, cell)

        if not ip_text:
            skipped.append({"row": offset, "reason": "缺少 IP"})
            continue
        ip = normalize_ip(ip_text)
        if not ip:
            skipped.append({"row": offset, "ip": ip_text, "reason": "IP 无法识别"})
            continue
        owners = index.get(ip) or []
        # 同一 IP 可能被 address 和 host_id 各登记一次，按 host_id 去重。
        unique: List[Dict[str, Any]] = []
        seen_ids = set()
        for host in owners:
            hid = str(host.get("host_id") or "")
            if hid in seen_ids:
                continue
            seen_ids.add(hid)
            unique.append(host)
        base = {
            "row": offset,
            "ip": ip,
            "patch": patch,
            "truncated": truncated,
        }
        if not unique:
            unmatched.append(dict(base, reason="没有 IP 对应的主机"))
            continue
        if len(unique) > 1:
            ambiguous.append(
                dict(
                    base,
                    reason="多台主机匹配到同一 IP",
                    host_ids=[h.get("host_id") for h in unique],
                )
            )
            continue
        host = unique[0]
        previous = seen_ip_row.get(ip)
        item = dict(
            base,
            host_id=host.get("host_id"),
            address=host.get("address") or "",
            hostname=host.get("hostname") or "",
            hidden=bool(host.get("hidden")),
            owner=(host.get("asset") or {}).get("owner") or host.get("owner") or "",
            purpose=(host.get("asset") or {}).get("purpose") or host.get("purpose") or "",
        )
        if previous:
            item["overrides_row"] = previous
        seen_ip_row[ip] = offset
        matched.append(item)

    # 同一 IP 多行时只应用最后一行，前面的行留在预览里标明被覆盖。
    applicable = []
    for item in matched:
        later = seen_ip_row.get(item["ip"])
        if later and later != item["row"]:
            item["skipped_apply"] = True
            continue
        item["skipped_apply"] = False
        if item["patch"]:
            applicable.append(item)

    return None, {
        "ignored_columns": ignored,
        "matched": matched,
        "unmatched": unmatched,
        "ambiguous": ambiguous,
        "skipped": skipped,
        "applicable": [
            {"host_id": item["host_id"], "row": item["row"], "patch": item["patch"]}
            for item in applicable
        ],
        "summary": {
            "matched": len(matched),
            "unmatched": len(unmatched),
            "ambiguous": len(ambiguous),
            "skipped": len(skipped),
            "will_update": len(applicable),
        },
    }


def _put_import_field(
    patch: Dict[str, str], truncated: List[str], key: str, value: str
) -> None:
    limit = ASSET_LIMITS[key]
    text = value.strip()
    if len(text) > limit:
        text = text[:limit]
        truncated.append(key)
    patch[key] = text


def iter_import_patches(plan: Dict[str, Any]) -> Iterable[Tuple[str, Dict[str, str]]]:
    for item in plan.get("applicable") or []:
        hid = str(item.get("host_id") or "").strip()
        patch = item.get("patch") or {}
        if hid and isinstance(patch, dict) and patch:
            yield hid, {k: str(v) for k, v in patch.items() if k in ASSET_FIELDS}
