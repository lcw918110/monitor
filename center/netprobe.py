"""从中心端探测到目标机的网络连通性。"""

from __future__ import annotations

import platform
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional


def tcp_probe(host: str, port: int, timeout: float = 2.0) -> Dict[str, Any]:
    started = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            rtt_ms = round((time.time() - started) * 1000, 1)
            return {"ok": True, "rtt_ms": rtt_ms, "error": ""}
    except OSError as exc:
        rtt_ms = round((time.time() - started) * 1000, 1)
        return {"ok": False, "rtt_ms": rtt_ms, "error": str(exc)}


def ping_probe(host: str, timeout: float = 2.0) -> Dict[str, Any]:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(int(timeout * 1000)), host]
    elif system == "darwin":
        cmd = ["ping", "-c", "1", "-W", str(int(timeout * 1000)), host]
    else:
        # Linux: -W 秒
        cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout))), host]

    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout + 1.5,
        )
        rtt_ms = round((time.time() - started) * 1000, 1)
        out = proc.stdout or ""
        # 尝试解析 time=
        import re

        m = re.search(r"time[=<]([\d.]+)\s*ms", out, re.I)
        if m:
            rtt_ms = float(m.group(1))
        return {
            "ok": proc.returncode == 0,
            "rtt_ms": rtt_ms if proc.returncode == 0 else None,
            "error": "" if proc.returncode == 0 else (out.strip().splitlines()[-1] if out.strip() else "ping failed"),
        }
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "rtt_ms": None, "error": str(exc)}


def probe_host(
    host: str,
    ssh_port: int = 22,
    extra_ports: Optional[List[int]] = None,
    timeout: float = 2.0,
) -> Dict[str, Any]:
    ports = []
    seen = set()
    for p in [int(ssh_port)] + list(extra_ports or []):
        if p not in seen:
            seen.add(p)
            ports.append(p)

    ping = ping_probe(host, timeout=timeout)
    tcp_results = {}
    for p in ports:
        tcp_results[str(p)] = tcp_probe(host, p, timeout=timeout)

    ssh = tcp_results.get(str(int(ssh_port))) or {"ok": False}
    # 综合：ping 或任一 TCP 通即视为可达
    reachable = bool(ping.get("ok")) or any(v.get("ok") for v in tcp_results.values())
    best_rtt = None
    if ping.get("ok") and ping.get("rtt_ms") is not None:
        best_rtt = ping["rtt_ms"]
    for v in tcp_results.values():
        if v.get("ok") and v.get("rtt_ms") is not None:
            best_rtt = v["rtt_ms"] if best_rtt is None else min(best_rtt, v["rtt_ms"])

    quality = "down"
    if reachable:
        if best_rtt is None:
            quality = "ok"
        elif best_rtt < 20:
            quality = "excellent"
        elif best_rtt < 80:
            quality = "good"
        elif best_rtt < 200:
            quality = "fair"
        else:
            quality = "poor"

    return {
        "host": host,
        "reachable": reachable,
        "quality": quality,
        "rtt_ms": best_rtt,
        "ping": ping,
        "tcp": tcp_results,
        "ssh_ok": bool(ssh.get("ok")),
        "probed_at": int(time.time()),
    }
