"""网络辅助：推断中心端对外可访问地址（供 Agent 上报）。"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from typing import List, Optional, Tuple
from urllib.parse import urlparse


def _is_private_lan(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    try:
        a, b = int(parts[0]), int(parts[1])
    except ValueError:
        return False
    if a == 10:
        return True
    if a == 192 and b == 168:
        return True
    if a == 172 and 16 <= b <= 31:
        return True
    return False


def _score_ip(ip: str) -> int:
    """分越高越适合作为「对外」局域网地址。"""
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return -100
    if ip.startswith("169.254."):
        return -50
    # 常见代理/虚拟网段（Clash 等常用 198.18.0.0/15）
    try:
        a, b = int(ip.split(".")[0]), int(ip.split(".")[1])
        if a == 198 and 18 <= b <= 19:
            return -40
    except (ValueError, IndexError):
        pass
    if _is_private_lan(ip):
        # 192.168 略优先于 10/172（家用/办公更常见）
        if ip.startswith("192.168."):
            return 100
        if ip.startswith("10."):
            return 90
        return 80
    # 其它公网地址：可用，但不如内网稳妥
    return 20


def _collect_ipv4_candidates() -> List[str]:
    found: List[str] = []

    def add(ip: str) -> None:
        ip = (ip or "").strip()
        if ip and ip not in found:
            found.append(ip)

    # 1) 默认出口（可能是 VPN）
    for probe in ("8.8.8.8", "223.5.5.5"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.3)
            s.connect((probe, 80))
            add(s.getsockname()[0])
            s.close()
        except OSError:
            continue

    # 2) hostname
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass

    # 3) ifconfig / ip（枚举网卡，优先真实局域网）
    cmds = [
        ["ifconfig"],
        ["ip", "-4", "-o", "addr", "show"],
    ]
    for cmd in cmds:
        try:
            out = subprocess.check_output(
                cmd, stderr=subprocess.DEVNULL, text=True, timeout=2
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for m in re.finditer(
            r"(?:inet |inet addr:)(\d+\.\d+\.\d+\.\d+)", out
        ):
            add(m.group(1))

    return found


def guess_lan_ip() -> Optional[str]:
    """推断本机局域网 IPv4（尽量避开 127.0.0.1 / VPN 虚拟地址）。"""
    cands = _collect_ipv4_candidates()
    if not cands:
        return None
    ranked: List[Tuple[int, str]] = sorted(
        ((_score_ip(ip), ip) for ip in cands), reverse=True
    )
    best_score, best_ip = ranked[0]
    if best_score < 0:
        return None
    return best_ip


def is_loopback_url(url: str) -> bool:
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlparse(raw if "://" in raw else "http://" + raw)
        host = (parsed.hostname or "").lower()
    except Exception:  # noqa: BLE001
        host = raw.lower()
    return host in ("127.0.0.1", "localhost", "::1") or host.startswith("127.")


def build_public_center_url(port: int, scheme: str = "http") -> Optional[str]:
    ip = guess_lan_ip()
    if not ip:
        return None
    return "%s://%s:%s" % (scheme, ip, int(port))


def ensure_public_center_url(store, port: int) -> str:
    """若未配置、为回环、或为劣质默认（如 VPN 198.18），则写入推断局域网地址。"""
    settings = store.get_settings(include_secrets=True)
    current = (settings.get("public_center_url") or "").strip()
    suggested = build_public_center_url(port)

    def _host(url: str) -> str:
        try:
            return (urlparse(url).hostname or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    if current and not is_loopback_url(current):
        # 已是正常局域网地址则保留；若当前像 VPN 段且能推断出更好的局域网 IP，则替换
        cur_host = _host(current)
        if suggested and _score_ip(cur_host) < 50 and _score_ip(_host(suggested)) >= 80:
            store.update_settings({"public_center_url": suggested})
            return suggested
        return current

    if not suggested:
        return current
    store.update_settings({"public_center_url": suggested})
    return suggested
