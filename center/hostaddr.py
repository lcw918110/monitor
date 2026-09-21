"""主机展示地址：优先完整 IPv4，避免短主机名或残缺 IP 片段。"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_PARTIAL_IPV4_RE = re.compile(r"^(?:\d{1,3}\.){1,2}\d{1,3}$")
# host-172-24-26-50 / host_172_24_26_50 → 172.24.26.50
_EMBEDDED_IPV4_RE = re.compile(
    r"(?:^|[^0-9])(\d{1,3})[-_.](\d{1,3})[-_.](\d{1,3})[-_.](\d{1,3})$"
)

_IFACE_IP_KEYS = ("ip", "ipv4", "addr", "address")
_SKIP_IFACE_PREFIXES = (
    "docker",
    "veth",
    "br-",
    "tun",
    "tap",
    "wg",
    "utun",
    "cni",
    "flannel",
    "cali",
)


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


def is_denied_display_ip(text: Any) -> bool:
    """不可作为管理/展示地址的 IPv4。

    含回环、``0.0.0.0/8``、链路本地 ``169.254.0.0/16``，以及
    ``198.18.0.0/15``（RFC 2544 / RFC 6890 基准测试网段）。后者常被
    Clash TUN、部分 VPN 用作 fake-IP / CGNAT 隧道地址，不是机器的
    真实 LAN / 管理 IP。
    """
    raw = str(text or "").strip()
    if not raw or not is_ipv4(raw):
        return True
    if is_loopback_ip(raw) or raw.startswith("0."):
        return True
    parts = [int(p) for p in raw.split(".")]
    a, b = parts[0], parts[1]
    if a == 169 and b == 254:
        return True
    # 198.18.0.0/15 → 198.18.0.0–198.19.255.255
    if a == 198 and b in (18, 19):
        return True
    return False


def is_rfc1918(text: Any) -> bool:
    """RFC 1918 私网（10/8、172.16/12、192.168/16）。不含 198.18/15。"""
    raw = str(text or "").strip()
    if not is_ipv4(raw) or is_denied_display_ip(raw):
        return False
    parts = [int(p) for p in raw.split(".")]
    a, b = parts[0], parts[1]
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


def normalize_ip(text: Any) -> str:
    """抽出可用的 IPv4（含 ``::ffff:a.b.c.d``）。

    回环、全零、链路本地、以及 ``198.18.0.0/15`` 隧道/假 IP 视为空。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith("::ffff:"):
        raw = raw[7:]
    if "%" in raw:
        raw = raw.split("%", 1)[0]
    if not is_ipv4(raw):
        return ""
    if is_denied_display_ip(raw):
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


def parse_host_id_ip(host_id: Any) -> str:
    """从 ``host-172-24-26-50`` 这类 host_id 抽出 IPv4；对不上则空。"""
    hid = str(host_id or "").strip()
    if not hid:
        return ""
    direct = normalize_ip(hid)
    if direct:
        return direct
    match = _EMBEDDED_IPV4_RE.search(hid)
    if not match:
        return ""
    return normalize_ip(".".join(match.groups()))


def _unique_ips(items: Iterable[Any]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        ip = normalize_ip(item)
        if not ip or ip in seen:
            continue
        seen.add(ip)
        out.append(ip)
    return out


def _iface_skipped(iface: Dict[str, Any]) -> bool:
    if iface.get("virtual") or iface.get("enslaved"):
        return True
    name = str(iface.get("name") or "").strip().lower()
    if not name or name == "lo":
        return True
    return any(name.startswith(prefix) for prefix in _SKIP_IFACE_PREFIXES)


def collect_payload_ips(payload: Optional[Dict[str, Any]] = None) -> List[str]:
    """Agent 上报里可用的 IPv4：``primary_ip`` / ``ipv4`` / ``ip``，再加非虚拟网卡。"""
    payload = payload or {}
    system = payload.get("system") or {}
    if not isinstance(system, dict):
        system = {}
    ordered: List[Any] = [
        system.get("primary_ip"),
        system.get("ipv4"),
        system.get("ip"),
    ]
    ifaces = system.get("net_ifaces")
    if not isinstance(ifaces, list):
        ifaces = payload.get("net_ifaces")
    if isinstance(ifaces, list):
        for iface in ifaces:
            if not isinstance(iface, dict) or _iface_skipped(iface):
                continue
            for key in _IFACE_IP_KEYS:
                ordered.append(iface.get(key))
    return _unique_ips(ordered)


def _prefer_lan(ips: Iterable[str]) -> str:
    items = [ip for ip in ips if ip]
    for ip in items:
        if is_rfc1918(ip):
            return ip
    return items[0] if items else ""


def resolve_host_address(
    host_id: Any = "",
    hostname: Any = "",
    payload: Optional[Dict[str, Any]] = None,
    remote_ip: Any = "",
    deploy_ip: Any = "",
    deploy_map: Optional[Dict[str, str]] = None,
) -> str:
    """解析列表应展示的完整地址，优先真实管理/LAN IPv4。

    来源顺序：

    1. 部署清单 ``deploy_targets.ip``（按 host_id / hostname 映射）
    2. ``host_id`` 内嵌 IP（如 ``host-172-24-26-50`` → ``172.24.26.50``）
    3. Agent 上报的 RFC1918 网卡/``primary_ip``（跳过 ``198.18/15`` 等假 IP）
    4. 其余非拒绝的上报 IP
    5. 上报来源 ``last_remote_ip``（同样走拒绝列表）
    6. ``hostname`` 本身若已是完整 IPv4
    """
    mapped = lookup_deploy_ip(host_id, hostname, deploy_map)
    for item in (deploy_ip, mapped):
        ip = normalize_ip(item)
        if ip:
            return ip

    hid_ip = parse_host_id_ip(host_id)
    if hid_ip:
        return hid_ip

    payload_ips = collect_payload_ips(payload)
    lan = _prefer_lan(payload_ips)
    if lan:
        return lan

    remote = normalize_ip(remote_ip)
    if remote:
        return remote

    hn = str(hostname or "").strip()
    if is_ipv4(hn):
        return normalize_ip(hn)
    return ""
