"""主机展示地址：优先完整 IPv4，避免短主机名或残缺 IP 片段。"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_PARTIAL_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){1,2}\d{1,3}$")


def is_ipv4(text: Any) -> bool:
    raw = str(text or "").strip()
    if not _IPV4_RE.match(raw):
        return False
    try:
        return all(0 <= int(part) <= 255 for part in raw.split("."))
    except ValueError:
        return False


def is_partial_ipv4(text: Any) -> bool:
    """一段或两段的 IPv4 残片，例如 ``15`` / ``192.168`` / ``192.168.15``。"""
    raw = str(text or "").strip()
    if not raw or is_ipv4(raw):
        return False
    if not _PARTIAL_IPV4_RE.match(raw):
        return False
    try:
        return all(0 <= int(part) <= 255 for part in raw.split("."))
    except ValueError:
        return False


def is_loopback_ip(text: Any) -> bool:
    raw = str(text or "").strip().lower()
    if not raw:
        return False
    if raw in ("::1", "localhost"):
        return True
    if raw.startswith("127."):
        return True
    return False


def normalize_ip(text: Any) -> str:
    """抽出可用的 IPv4（含 ``::ffff:a.b.c.d``），回环/链路本地视为空。"""
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("::ffff:"):
        raw = raw[7:]
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    if not is_ipv4(raw):
        return ""
    if is_loopback_ip(raw) or raw.startswith("0.") or raw.startswith("169.254."):
        return ""
    return raw


def is_short_hostname(text: Any) -> bool:
    """单段主机名（无点），或残缺 IP，不适合单独当作地址。"""
    raw = str(text or "").strip()
    if not raw:
        return True
    if is_partial_ipv4(raw):
        return True
    if is_ipv4(raw):
        return False
    return "." not in raw


def lookup_deploy_ip(
    host_id: Any,
    hostname: Any,
    deploy_map: Optional[Dict[str, str]] = None,
) -> str:
    mapping = deploy_map or {}
    for key in (host_id, hostname):
        ip = normalize_ip(mapping.get(str(key or "").strip()))
        if ip:
            return ip
    return ""


def resolve_host_address(
    host_id: Any = "",
    hostname: Any = "",
    payload: Optional[Dict[str, Any]] = None,
    remote_ip: Any = "",
    deploy_ip: Any = "",
    deploy_map: Optional[Dict[str, str]] = None,
) -> str:
    """解析列表应展示的完整地址，优先 IPv4。

    来源顺序：Agent ``system.primary_ip`` → 部署清单 IP → 上报来源 IP
    → ``host_id`` / ``hostname`` 本身若已是完整 IPv4。
    """
    system = (payload or {}).get("system") or {}
    if not isinstance(system, dict):
        system = {}
    mapped = lookup_deploy_ip(host_id, hostname, deploy_map)
    candidates = [
        system.get("primary_ip"),
        system.get("ipv4"),
        system.get("ip"),
        deploy_ip,
        mapped,
        remote_ip,
    ]
    hid = str(host_id or "").strip()
    hn = str(hostname or "").strip()
    if is_ipv4(hid):
        candidates.append(hid)
    if is_ipv4(hn):
        candidates.append(hn)
    for item in candidates:
        ip = normalize_ip(item)
        if ip:
            return ip
    return ""
