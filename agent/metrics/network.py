"""网络接口吞吐采样（Linux /proc/net/dev；失败则返回空）。"""

from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple


def _read_net_counters() -> Optional[Tuple[int, int]]:
    try:
        with open("/proc/net/dev", "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError:
        return None

    rx = 0
    tx = 0
    for line in lines[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        iface = name.strip()
        if iface == "lo":
            continue
        parts = rest.split()
        if len(parts) < 9:
            continue
        try:
            rx += int(parts[0])
            tx += int(parts[8])
        except ValueError:
            continue
    return rx, tx


def collect_network(sample_sec: float = 0.4) -> Dict[str, Any]:
    a = _read_net_counters()
    if not a:
        return {
            "net_rx_mbps": None,
            "net_tx_mbps": None,
            "net_rx_bytes": None,
            "net_tx_bytes": None,
        }
    time.sleep(max(0.1, sample_sec))
    b = _read_net_counters()
    if not b:
        return {
            "net_rx_mbps": None,
            "net_tx_mbps": None,
            "net_rx_bytes": a[0],
            "net_tx_bytes": a[1],
        }
    dt = max(sample_sec, 0.1)
    rx_bps = max(0, b[0] - a[0]) / dt
    tx_bps = max(0, b[1] - a[1]) / dt
    return {
        "net_rx_mbps": round(rx_bps * 8 / 1_000_000, 3),
        "net_tx_mbps": round(tx_bps * 8 / 1_000_000, 3),
        "net_rx_bytes": b[0],
        "net_tx_bytes": b[1],
    }
