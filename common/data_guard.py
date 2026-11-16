"""重点数据守护的纯判定。

只根据路径清点（个数、大小、时间）、已有的发送速率、连接摘要和挂载摘要做判断。
不读文件内容，不抓包，不拦截。界面、导出和诊断共用下面这套名字，不要另起同义词。

- 重点数据守护：功能名
- 重点数据机：被显式标进范围的主机
- 数据外传异常：今日关注里的那一类（下面四项里任意一项成立）
- 外传异常：发送速率或一段时间的发送量超过设置
- 路径完整性变化：受保护路径突然变小，或旁边/底下突然出现大归档
- 未允许网段外连：连到允许网段和白名单之外
- 可移动磁盘挂载：U 盘一类的挂载
"""

import hashlib
import ipaddress
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


KIND_EGRESS = "egress_anomaly"
KIND_PATH = "path_integrity"
KIND_CIDR = "unallowed_cidr"
KIND_REMOVABLE = "removable_mount"
KINDS = (KIND_EGRESS, KIND_PATH, KIND_CIDR, KIND_REMOVABLE)

LABEL_FEATURE = "重点数据守护"
LABEL_HOST = "重点数据机"
LABEL_ATTENTION = "数据外传异常"
KIND_LABELS = {
    KIND_EGRESS: "外传异常",
    KIND_PATH: "路径完整性变化",
    KIND_CIDR: "未允许网段外连",
    KIND_REMOVABLE: "可移动磁盘挂载",
}

LIMITATION = (
    "重点数据守护只发现并提示重点数据可能正在离开的迹象，不能保证数据出不去。"
    "拦截属于防火墙和权限。它看不到加密隧道里的内容，也不能指出究竟是哪一个文件离开了。"
    "备份和同步若是正常业务，必须把目的地址或进程写进白名单，否则会报数据外传异常。"
)

# 这些键名出现在报文里就丢掉。total_bytes、archive_bytes 不含在内。
_CONTENT_KEYS = {
    "content",
    "contents",
    "file_content",
    "file_contents",
    "body",
    "text",
    "data",
    "payload",
    "packet",
    "packets",
    "preview",
    "raw",
    "blob",
    "b64",
    "base64",
    "file_text",
    "file_bytes",
}

_MAX_STRING = 512
_MAX_DESTINATIONS = 20
_MAX_MOUNTS = 8
_MAX_PATH_EVENTS = 32

ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tbz2",
    ".txz",
    ".tgz",
    ".tar",
    ".zip",
    ".7z",
    ".rar",
    ".iso",
)


def empty_counts(marked=False):
    # type: (bool) -> Dict[str, Any]
    out = {"marked": bool(marked)}  # type: Dict[str, Any]
    for kind in KINDS:
        out["%s_count" % kind] = 0
        out["%s_seconds" % kind] = 0
    return out


def _bad_key(key):
    # type: (Any) -> bool
    text = str(key or "").lower().replace("-", "_")
    if text in _CONTENT_KEYS:
        return True
    if "content" in text or "packet" in text:
        return True
    if text.endswith("_b64") or text.endswith("_base64"):
        return True
    return False


def sanitize(value, depth=0):
    # type: (Any, int) -> Any
    """丢掉文件内容、报文和超长字符串。清单数字和路径保留。"""
    if depth > 8:
        return None
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if _bad_key(key):
                continue
            out[str(key)] = sanitize(item, depth + 1)
        return out
    if isinstance(value, list):
        return [sanitize(item, depth + 1) for item in value[:600]]
    if isinstance(value, str):
        if len(value) > _MAX_STRING:
            return ""
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return None


def _as_int(value):
    # type: (Any) -> Optional[int]
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value):
    # type: (Any) -> Optional[float]
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _lines(value):
    # type: (Any) -> List[str]
    if value is None:
        return []
    if isinstance(value, str):
        parts = value.replace(",", "\n").splitlines()
    elif isinstance(value, list):
        parts = value
    else:
        return []
    out = []
    seen = set()
    for item in parts:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def parse_network(text):
    # type: (str) -> Optional[Any]
    raw = str(text or "").strip()
    if not raw:
        return None
    if "/" not in raw:
        try:
            ipaddress.ip_address(raw)
        except ValueError:
            return None
        raw = raw + ("/32" if ":" not in raw else "/128")
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None


def ip_in_networks(ip_text, networks):
    # type: (str, Sequence[Any]) -> bool
    try:
        ip = ipaddress.ip_address(str(ip_text or "").strip())
    except ValueError:
        return False
    for network in networks:
        if network is None:
            continue
        try:
            if ip in network:
                return True
        except TypeError:
            continue
    return False


def _clean_path(text):
    # type: (str) -> Optional[str]
    path = str(text or "").strip().replace("\\", "/")
    if not path.startswith("/"):
        return None
    if len(path) > 512:
        return None
    parts = [part for part in path.split("/") if part not in ("", ".")]
    if not parts or ".." in parts:
        return None
    if path == "/":
        return None
    return "/" + "/".join(parts)


def _clean_process(text):
    # type: (str) -> Optional[str]
    name = str(text or "").strip().replace("\\", "/").split("/")[-1]
    if not name or len(name) > 64:
        return None
    if any(ch in name for ch in " \t\r\n/"):
        return None
    return name


def normalize_policy(raw):
    # type: (Any) -> Tuple[Optional[Dict[str, Any]], Optional[str]]
    """把设置页提交收成一份策略。出错时返回中文原因。"""
    if not isinstance(raw, dict):
        return None, "重点数据守护的配置必须是对象"
    paths = []
    for item in _lines(raw.get("paths")):
        cleaned = _clean_path(item)
        if cleaned is None:
            return None, "受保护路径必须是本机绝对路径，不能是根目录，也不能含 ..：%s" % item
        if cleaned not in paths:
            paths.append(cleaned)
        if len(paths) > 32:
            return None, "受保护路径最多 32 条"
    allowed = []
    for item in _lines(raw.get("allowed_cidrs")):
        network = parse_network(item)
        if network is None:
            return None, "允许网段不是合法的 IP 或 CIDR：%s" % item
        text = str(network)
        if text not in allowed:
            allowed.append(text)
        if len(allowed) > 32:
            return None, "允许网段最多 32 条"
    allow_dest = []
    for item in _lines(raw.get("allow_destinations")):
        network = parse_network(item)
        if network is None:
            return None, "白名单目的地址不是合法的 IP 或 CIDR：%s" % item
        text = str(network)
        if text not in allow_dest:
            allow_dest.append(text)
        if len(allow_dest) > 32:
            return None, "白名单目的地址最多 32 条"
    processes = []
    for item in _lines(raw.get("allow_processes")):
        name = _clean_process(item)
        if name is None:
            return None, "白名单进程只写进程名，不要带路径或空格：%s" % item
        if name not in processes:
            processes.append(name)
        if len(processes) > 32:
            return None, "白名单进程最多 32 个"

    def _optional_number(key, low, high, label):
        # type: (str, float, float, str) -> Tuple[Optional[float], Optional[str]]
        if key not in raw or raw.get(key) in (None, ""):
            return None, None
        number = _as_float(raw.get(key))
        if number is None:
            return None, "%s必须是数字" % label
        if number < low or number > high:
            return None, "%s超出范围" % label
        return number, None

    egress_mbps, err = _optional_number("egress_mbps", 0.1, 100000, "外传速率阈值")
    if err:
        return None, err
    volume_mb, err = _optional_number("egress_volume_mb", 1, 10000000, "外传量阈值")
    if err:
        return None, err
    baseline, err = _optional_number("baseline_multiplier", 1, 100, "相对平时发送的倍数")
    if err:
        return None, err
    sustain = _as_int(raw.get("egress_sustain_seconds"))
    if sustain is None:
        sustain = 60
    if sustain < 15 or sustain > 3600:
        return None, "外传速率要连续保持的秒数应在 15 到 3600 之间"
    window = _as_int(raw.get("egress_volume_window_seconds"))
    if window is None:
        window = 3600
    if window < 60 or window > 86400:
        return None, "外传量的统计秒数应在 60 到 86400 之间"
    shrink_ratio = _as_float(raw.get("shrink_ratio"))
    if shrink_ratio is None:
        shrink_ratio = 0.5
    if shrink_ratio <= 0 or shrink_ratio > 1:
        return None, "路径变小的比例应在 0 到 1 之间"
    archive_min = _as_int(raw.get("archive_min_bytes"))
    if archive_min is None:
        archive_min = 100 * 1024 * 1024
    if archive_min < 1:
        return None, "大归档的最小字节数至少为 1"
    enabled = raw.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() in ("1", "true", "yes", "on")
    policy = {
        "enabled": bool(enabled),
        "paths": paths,
        "allowed_cidrs": allowed,
        "allow_destinations": allow_dest,
        "allow_processes": processes,
        "egress_mbps": egress_mbps,
        "egress_sustain_seconds": int(sustain),
        "egress_volume_mb": volume_mb,
        "egress_volume_window_seconds": int(window),
        "baseline_multiplier": baseline,
        "shrink_ratio": float(shrink_ratio),
        "archive_min_bytes": int(archive_min),
    }
    return policy, None


def public_policy(policy):
    # type: (Optional[Dict[str, Any]]) -> Dict[str, Any]
    if not policy or not policy.get("enabled"):
        return {"enabled": False}
    kept = dict(policy)
    kept["enabled"] = True
    return kept


def is_archive_name(name):
    # type: (str) -> bool
    lower = str(name or "").lower()
    for suffix in ARCHIVE_SUFFIXES:
        if lower.endswith(suffix):
            return True
    return False


def _basename(name):
    # type: (str) -> str
    text = str(name or "").replace("\\", "/").split("/")[-1].strip()
    if len(text) > 180:
        text = text[:180]
    return text


def detect_path_changes(current, previous, policy):
    # type: (Dict[str, Any], Optional[Dict[str, Any]], Dict[str, Any]) -> List[Dict[str, Any]]
    """当前清点和上一笔对比。第一笔只建立基线，不报。读失败或没扫完也不报变小。"""
    events = []  # type: List[Dict[str, Any]]
    path = str(current.get("path") or "")
    if current.get("error") or current.get("complete") is False:
        return events
    file_count = _as_int(current.get("file_count"))
    total_bytes = _as_int(current.get("total_bytes"))
    if file_count is None or total_bytes is None:
        return events
    if not isinstance(previous, dict) or previous.get("complete") is False:
        return events
    if previous.get("error"):
        return events
    prev_count = _as_int(previous.get("file_count"))
    prev_bytes = _as_int(previous.get("total_bytes"))
    ratio = _as_float(policy.get("shrink_ratio")) or 0.5
    min_files = _as_int(policy.get("shrink_min_files"))
    if min_files is None:
        min_files = 4
    min_bytes = _as_int(policy.get("shrink_min_bytes"))
    if min_bytes is None:
        min_bytes = 1024 * 1024
    if (
        prev_count is not None
        and prev_bytes is not None
        and prev_count >= min_files
        and prev_bytes >= min_bytes
        and file_count <= prev_count * (1.0 - ratio)
        and total_bytes <= prev_bytes * (1.0 - ratio)
    ):
        events.append(
            {
                "kind": KIND_PATH,
                "reason": "shrink",
                "path": path,
                "file_count": file_count,
                "previous_file_count": prev_count,
                "total_bytes": total_bytes,
                "previous_total_bytes": prev_bytes,
            }
        )
    archive_min = _as_int(policy.get("archive_min_bytes")) or (100 * 1024 * 1024)
    seen = set()
    for item in previous.get("archives") or []:
        if isinstance(item, str):
            seen.add(item)
        elif isinstance(item, dict):
            seen.add(_basename(item.get("name") or ""))
    for item in current.get("new_archives") or []:
        if not isinstance(item, dict):
            continue
        name = _basename(item.get("name") or "")
        size = _as_int(item.get("bytes"))
        if not name or not is_archive_name(name) or size is None or size < archive_min:
            continue
        if name in seen:
            continue
        where = item.get("where") if item.get("where") in ("under", "beside") else "under"
        events.append(
            {
                "kind": KIND_PATH,
                "reason": "archive",
                "path": path,
                "archive_name": name,
                "archive_bytes": size,
                "where": where,
            }
        )
        if len(events) >= _MAX_PATH_EVENTS:
            break
    return events


def _networks(texts):
    # type: (Iterable[str]) -> List[Any]
    out = []
    for text in texts or []:
        network = parse_network(text)
        if network is not None:
            out.append(network)
    return out


def _process_allowed(process, policy):
    # type: (Any, Dict[str, Any]) -> bool
    name = _clean_process(process or "")
    if not name:
        return False
    return name in set(policy.get("allow_processes") or [])


def _dest_allowlisted(ip_text, policy):
    # type: (str, Dict[str, Any]) -> bool
    return ip_in_networks(ip_text, _networks(policy.get("allow_destinations") or []))


def _dest_in_allowed_cidr(ip_text, policy):
    # type: (str, Dict[str, Any]) -> bool
    return ip_in_networks(ip_text, _networks(policy.get("allowed_cidrs") or []))


def connection_is_benign(conn, policy):
    # type: (Dict[str, Any], Dict[str, Any]) -> bool
    if _process_allowed(conn.get("process"), policy):
        return True
    ip_text = str(conn.get("ip") or "")
    if _dest_allowlisted(ip_text, policy):
        return True
    if policy.get("allowed_cidrs") and _dest_in_allowed_cidr(ip_text, policy):
        return True
    return False


def classify_connections(connections, policy):
    # type: (Any, Dict[str, Any]) -> Optional[Dict[str, Any]]
    """connections 为 None 表示没读到，不把「没读到」当成没有外连。"""
    if connections is None:
        return None
    if not policy.get("allowed_cidrs"):
        return None
    destinations = []
    total = 0
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        if connection_is_benign(conn, policy):
            continue
        ip_text = str(conn.get("ip") or "")
        if _dest_in_allowed_cidr(ip_text, policy):
            continue
        count = _as_int(conn.get("count")) or 1
        total += count
        if len(destinations) < _MAX_DESTINATIONS:
            destinations.append(
                {
                    "ip": ip_text,
                    "port": _as_int(conn.get("port")) or 0,
                    "count": count,
                    "process": _clean_process(conn.get("process") or "") or "",
                }
            )
    if total <= 0:
        return None
    return {
        "kind": KIND_CIDR,
        "connection_count": total,
        "destinations": destinations,
    }


def _median(values):
    # type: (Sequence[float]) -> float
    ordered = sorted(values)
    count = len(ordered)
    mid = count // 2
    if count % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def evaluate_egress(samples, policy, now):
    # type: (Sequence[Dict[str, Any]], Dict[str, Any], int) -> Optional[Dict[str, Any]]
    """用已有的 net_tx（Mbps）判断持续速率、窗口发送量和相对平时的倍数。"""
    points = []
    for item in samples or []:
        if not isinstance(item, dict):
            continue
        ts = _as_int(item.get("ts"))
        mbps = _as_float(item.get("mbps"))
        if ts is None or mbps is None or mbps < 0:
            continue
        points.append((ts, mbps))
    points.sort()
    if not points:
        return None
    now = int(now)
    reasons = []
    threshold = _as_float(policy.get("egress_mbps"))
    sustain = _as_int(policy.get("egress_sustain_seconds")) or 60
    latest = points[-1][1]
    sustained_min = None
    if threshold is not None and threshold > 0:
        window = [item for item in points if now - sustain <= item[0] <= now]
        if len(window) >= 2:
            span = window[-1][0] - window[0][0]
            sustained_min = min(item[1] for item in window)
            if span + 1 >= sustain and sustained_min >= threshold:
                reasons.append("mbps")
    volume_limit = _as_float(policy.get("egress_volume_mb"))
    volume_window = _as_int(policy.get("egress_volume_window_seconds")) or 3600
    volume_mb = 0.0
    if volume_limit is not None and volume_limit > 0 and len(points) >= 2:
        for index in range(1, len(points)):
            prev_ts, prev_mbps = points[index - 1]
            ts, _mbps = points[index]
            if ts < now - volume_window or prev_ts > now:
                continue
            delta = ts - prev_ts
            if delta <= 0 or delta > volume_window:
                continue
            volume_mb += prev_mbps * float(delta) / 8.0
        if volume_mb >= volume_limit:
            reasons.append("volume")
    multiplier = _as_float(policy.get("baseline_multiplier"))
    if multiplier is not None and multiplier > 0 and len(points) >= 5:
        older = [item[1] for item in points if item[0] < now - sustain]
        recent = [item for item in points if item[0] >= now - sustain]
        if len(older) >= 3 and len(recent) >= 2:
            base = _median(older)
            span = recent[-1][0] - recent[0][0]
            if base > 0 and span + 1 >= sustain and min(item[1] for item in recent) >= base * multiplier:
                reasons.append("baseline")
    if not reasons:
        return None
    return {
        "kind": KIND_EGRESS,
        "net_tx_mbps": round(latest, 2),
        "sustain_seconds": sustain,
        "volume_mb": round(volume_mb, 2),
        "threshold_mbps": threshold,
        "threshold_volume_mb": volume_limit,
        "reasons": reasons,
    }


def _allowlisted_process_running(observation, policy):
    # type: (Dict[str, Any], Dict[str, Any]) -> bool
    wanted = set(policy.get("allow_processes") or [])
    if not wanted:
        return False
    seen = observation.get("allowlisted_processes") or []
    for name in seen:
        if _clean_process(name or "") in wanted:
            return True
    for conn in observation.get("connections") or []:
        if isinstance(conn, dict) and _process_allowed(conn.get("process"), policy):
            return True
    return False


def _egress_suppressed(observation, policy):
    # type: (Dict[str, Any], Dict[str, Any]) -> bool
    connections = observation.get("connections")
    if not isinstance(connections, list) or not connections:
        return False
    for conn in connections:
        if not isinstance(conn, dict):
            return False
        if not connection_is_benign(conn, policy):
            return False
    return True


def evaluate(policy, observation, ts=None):
    # type: (Dict[str, Any], Any, Optional[int]) -> List[Dict[str, Any]]
    """对一份清点结果给出事件。策略未启用时返回空。"""
    if not isinstance(policy, dict) or not policy.get("enabled"):
        return []
    observation = sanitize(observation if isinstance(observation, dict) else {})
    events = []  # type: List[Dict[str, Any]]
    for item in observation.get("paths") or []:
        if not isinstance(item, dict):
            continue
        previous = item.get("previous") if isinstance(item.get("previous"), dict) else None
        events.extend(detect_path_changes(item, previous, policy))
    cidr_event = classify_connections(observation.get("connections"), policy)
    if cidr_event:
        events.append(cidr_event)
    sample_now = ts
    if sample_now is None:
        for item in observation.get("tx_samples") or []:
            if isinstance(item, dict):
                stamp = _as_int(item.get("ts"))
                if stamp is not None and (sample_now is None or stamp > sample_now):
                    sample_now = stamp
    egress = evaluate_egress(
        observation.get("tx_samples") or [], policy, int(sample_now or 0)
    )
    if egress and not _egress_suppressed(observation, policy):
        events.append(egress)
    mounts = observation.get("mounts")
    if isinstance(mounts, list) and mounts and not _allowlisted_process_running(observation, policy):
        cleaned = []
        for mount in mounts[:_MAX_MOUNTS]:
            if not isinstance(mount, dict):
                continue
            cleaned.append(
                {
                    "mount": str(mount.get("mount") or "")[:256],
                    "device": str(mount.get("device") or "")[:128],
                    "fstype": str(mount.get("fstype") or "")[:32],
                }
            )
        if cleaned:
            events.append({"kind": KIND_REMOVABLE, "mounts": cleaned})
    stamped = []
    for event in events:
        item = sanitize(event)
        if not isinstance(item, dict) or item.get("kind") not in KINDS:
            continue
        if ts is not None:
            item["ts"] = int(ts)
        item["id"] = event_id(item)
        stamped.append(item)
    return stamped


def event_id(event):
    # type: (Dict[str, Any]) -> str
    dests = event.get("destinations") or []
    dest_key = ",".join(
        "%s:%s" % (item.get("ip"), item.get("port"))
        for item in dests
        if isinstance(item, dict)
    )
    mounts = event.get("mounts") or []
    mount_key = ",".join(
        str(item.get("mount") or "") for item in mounts if isinstance(item, dict)
    )
    material = "|".join(
        [
            str(event.get("kind") or ""),
            str(event.get("ts") or ""),
            str(event.get("reason") or ""),
            str(event.get("path") or ""),
            str(event.get("archive_name") or ""),
            dest_key,
            mount_key,
            ",".join(str(item) for item in (event.get("reasons") or [])),
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def event_label(event):
    # type: (Dict[str, Any]) -> str
    kind = event.get("kind")
    label = KIND_LABELS.get(kind, LABEL_ATTENTION)
    if kind == KIND_PATH and event.get("reason") == "shrink":
        return "%s：%s 从 %s 个 / %s 字节减到 %s 个 / %s 字节" % (
            label,
            event.get("path") or "-",
            event.get("previous_file_count"),
            event.get("previous_total_bytes"),
            event.get("file_count"),
            event.get("total_bytes"),
        )
    if kind == KIND_PATH and event.get("reason") == "archive":
        where = "路径下" if event.get("where") == "under" else "路径旁"
        return "%s：%s出现大归档 %s（%s 字节）" % (
            label,
            where,
            event.get("archive_name") or "-",
            event.get("archive_bytes"),
        )
    if kind == KIND_EGRESS:
        return "%s：发送 %s Mbps，窗口发送约 %s MB" % (
            label,
            event.get("net_tx_mbps"),
            event.get("volume_mb"),
        )
    if kind == KIND_CIDR:
        return "%s：%s 条连接不在允许网段内" % (label, event.get("connection_count") or 0)
    if kind == KIND_REMOVABLE:
        mounts = event.get("mounts") or []
        names = "、".join(str(item.get("mount") or "") for item in mounts if isinstance(item, dict))
        return "%s：%s" % (label, names or "-")
    return label


def summarize_timestamps(sample_ts, active_ts, offline_seconds):
    # type: (Sequence[int], Sequence[int], int) -> Tuple[int, int]
    """次数是出现该情况的上报点个数。时长只把间隔不超过离线阈值的相邻点连起来。"""
    active = set(int(item) for item in active_ts)
    timeline = sorted(set(int(item) for item in sample_ts) | active)
    if not active:
        return 0, 0
    total = 0
    offline_seconds = max(1, int(offline_seconds))
    for index in range(len(timeline) - 1):
        if timeline[index] not in active:
            continue
        delta = timeline[index + 1] - timeline[index]
        if delta <= 0 or delta > offline_seconds:
            continue
        total += delta
    return len(active), total


def add_counts(left, right):
    # type: (Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]
    out = empty_counts(bool(left.get("marked") or right.get("marked")))
    for kind in KINDS:
        count_key = "%s_count" % kind
        seconds_key = "%s_seconds" % kind
        out[count_key] = int(left.get(count_key) or 0) + int(right.get(count_key) or 0)
        out[seconds_key] = int(left.get(seconds_key) or 0) + int(right.get(seconds_key) or 0)
    return out


def inventory_public(paths):
    # type: (Any) -> List[Dict[str, Any]]
    rows = []
    for item in paths or []:
        if not isinstance(item, dict):
            continue
        file_count = _as_int(item.get("file_count")) if not item.get("error") else None
        total_bytes = _as_int(item.get("total_bytes")) if not item.get("error") else None
        newest = _as_int(item.get("newest_mtime")) if not item.get("error") else None
        rows.append(
            {
                "path": str(item.get("path") or "")[:512],
                "file_count": file_count,
                "total_bytes": total_bytes,
                "newest_mtime": newest,
                "complete": False if item.get("error") else bool(item.get("complete")),
                "error": str(item.get("error") or "")[:64],
                "meta_fingerprint": item.get("meta_fingerprint") or None,
            }
        )
    return rows
