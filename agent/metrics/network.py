"""网络接口吞吐采样（Linux /proc/net/dev）与额定链路速率。

实时：采样间隔内的 rx/tx 比特率（Mbps）。
额定：sysfs `speed`（必要时 ethtool）给出的链路速率；利用率 = 实时/额定。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# 容器/虚拟网卡：主机上计入会虚高额定带宽；容器内主网卡通常叫 eth0 而非 veth*
_VIRTUAL_PREFIXES = (
    "lo",
    "docker",
    "veth",
    "br-",
    "virbr",
    "tun",
    "tap",
    "wg",
    "cni",
    "flannel",
    "cali",
    "kube",
    "nodelocal",
    "dummy",
    "sit",
    "ip6tnl",
    "gre",
    "gretap",
    "erspan",
    "vcan",
    "ifb",
    "nerdctl",
)

_ETHTOOL_SPEED_RE = re.compile(
    r"Speed:\s*(\d+(?:\.\d+)?)\s*(Mb/s|Mb/sec|Mbps|Gb/s|Gb/sec|Gbps)",
    re.I,
)


def iface_is_virtual(name: str) -> bool:
    n = (name or "").strip()
    if not n or n == "lo":
        return True
    lower = n.lower()
    for prefix in _VIRTUAL_PREFIXES:
        if prefix == "lo":
            continue
        if lower.startswith(prefix):
            return True
    return False


def parse_proc_net_dev(text: str) -> Dict[str, Tuple[int, int]]:
    """iface -> (rx_bytes, tx_bytes)。跳过表头与无法解析的行。"""
    counters: Dict[str, Tuple[int, int]] = {}
    for line in (text or "").splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        iface = name.strip()
        if not iface:
            continue
        parts = rest.split()
        if len(parts) < 9:
            continue
        try:
            counters[iface] = (int(parts[0]), int(parts[8]))
        except ValueError:
            continue
    return counters


def parse_sysfs_speed(raw: Optional[str]) -> Optional[int]:
    """sysfs speed 单位为 Mbps；-1 / Unknown 视为不可用。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text or text.startswith("-") or "unknown" in text.lower():
        return None
    try:
        val = int(float(text.split()[0]))
    except (TypeError, ValueError):
        return None
    return val if val > 0 else None


def parse_ethtool_speed(text: str) -> Optional[int]:
    """解析 `ethtool` 输出中的 Speed 行，统一为 Mbps。"""
    m = _ETHTOOL_SPEED_RE.search(text or "")
    if not m:
        return None
    try:
        n = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2).lower()
    if unit.startswith("g"):
        n *= 1000.0
    val = int(round(n))
    return val if val > 0 else None


def net_util_percent(rate_mbps: Optional[float], rated_mbps: Optional[float]) -> Optional[float]:
    if rate_mbps is None or rated_mbps is None:
        return None
    try:
        rated = float(rated_mbps)
        rate = float(rate_mbps)
    except (TypeError, ValueError):
        return None
    if rated <= 0:
        return None
    return round(max(0.0, min(100.0, rate * 100.0 / rated)), 2)


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def _iface_enslaved(sys_class_net: str, name: str) -> bool:
    return os.path.lexists(os.path.join(sys_class_net, name, "master"))


def _ethtool_speed(name: str) -> Optional[int]:
    if not shutil.which("ethtool"):
        return None
    try:
        out = subprocess.check_output(
            ["ethtool", name],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_ethtool_speed(out)


def list_iface_links(
    sys_class_net: str = "/sys/class/net",
    names: Optional[Iterable[str]] = None,
    ethtool_fn=None,
) -> List[Dict[str, Any]]:
    """读取各网卡 operstate / speed。ethtool_fn 仅在 sysfs 无速率时调用。"""
    if names is None:
        try:
            names = sorted(os.listdir(sys_class_net))
        except OSError:
            names = []
    out: List[Dict[str, Any]] = []
    for name in names:
        if not name or name == "lo":
            continue
        base = os.path.join(sys_class_net, name)
        oper = (_read_text(os.path.join(base, "operstate")) or "").strip().lower()
        speed = parse_sysfs_speed(_read_text(os.path.join(base, "speed")))
        if speed is None and ethtool_fn is not None:
            extra = ethtool_fn(name)
            if isinstance(extra, int):
                speed = extra if extra > 0 else None
            elif extra:
                speed = parse_ethtool_speed(str(extra)) or parse_sysfs_speed(str(extra))
        out.append(
            {
                "name": name,
                "up": oper == "up",
                "speed_mbps": speed,
                "virtual": iface_is_virtual(name),
                "enslaved": _iface_enslaved(sys_class_net, name),
            }
        )
    return out


def select_rated_ifaces(ifaces: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """额定带宽只计非虚拟、非 enslaved 的网卡；若过滤后为空则回退到全部非 lo。"""
    preferred = [
        i
        for i in ifaces
        if not i.get("virtual") and not i.get("enslaved") and i.get("name") != "lo"
    ]
    if preferred:
        return preferred
    return [i for i in ifaces if i.get("name") != "lo"]


def aggregate_rated(ifaces: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对选定网卡汇总额定带宽：UP 且 speed>0 的速率求和；主链路取其中最大。"""
    rated_ifaces = select_rated_ifaces(ifaces)
    up_with_speed = [
        i
        for i in rated_ifaces
        if i.get("up") and i.get("speed_mbps") and int(i["speed_mbps"]) > 0
    ]
    speeds = [int(i["speed_mbps"]) for i in up_with_speed]
    rated = sum(speeds) if speeds else None
    link = max(speeds) if speeds else None
    return {
        "net_rated_mbps": rated,
        "net_link_mbps": link,
        "net_ifaces": [
            {
                "name": i.get("name"),
                "up": bool(i.get("up")),
                "speed_mbps": i.get("speed_mbps"),
                "virtual": bool(i.get("virtual")),
                "enslaved": bool(i.get("enslaved")),
            }
            for i in rated_ifaces
        ],
    }


def _read_net_counters() -> Optional[Tuple[int, int]]:
    text = _read_text("/proc/net/dev")
    if text is None:
        return None
    counters = parse_proc_net_dev(text)
    if not counters:
        return None
    rx = 0
    tx = 0
    for name, (r, t) in counters.items():
        if name == "lo":
            continue
        rx += r
        tx += t
    return rx, tx


def _empty_network() -> Dict[str, Any]:
    return {
        "net_rx_mbps": None,
        "net_tx_mbps": None,
        "net_rx_bytes": None,
        "net_tx_bytes": None,
        "net_rated_mbps": None,
        "net_link_mbps": None,
        "net_rx_percent": None,
        "net_tx_percent": None,
        "net_ifaces": [],
    }


def collect_network(
    sample_sec: float = 0.4,
    sys_class_net: str = "/sys/class/net",
    use_ethtool: bool = True,
) -> Dict[str, Any]:
    rated = aggregate_rated(
        list_iface_links(
            sys_class_net,
            ethtool_fn=_ethtool_speed if use_ethtool else None,
        )
    )
    a = _read_net_counters()
    if not a:
        out = _empty_network()
        out.update(rated)
        return out
    time.sleep(max(0.1, sample_sec))
    b = _read_net_counters()
    if not b:
        out = _empty_network()
        out.update(rated)
        out["net_rx_bytes"] = a[0]
        out["net_tx_bytes"] = a[1]
        return out
    dt = max(sample_sec, 0.1)
    rx_bps = max(0, b[0] - a[0]) / dt
    tx_bps = max(0, b[1] - a[1]) / dt
    rx_mbps = round(rx_bps * 8 / 1_000_000, 3)
    tx_mbps = round(tx_bps * 8 / 1_000_000, 3)
    rated_mbps = rated.get("net_rated_mbps")
    return {
        "net_rx_mbps": rx_mbps,
        "net_tx_mbps": tx_mbps,
        "net_rx_bytes": b[0],
        "net_tx_bytes": b[1],
        "net_rated_mbps": rated_mbps,
        "net_link_mbps": rated.get("net_link_mbps"),
        "net_rx_percent": net_util_percent(rx_mbps, rated_mbps),
        "net_tx_percent": net_util_percent(tx_mbps, rated_mbps),
        "net_ifaces": rated.get("net_ifaces") or [],
    }
