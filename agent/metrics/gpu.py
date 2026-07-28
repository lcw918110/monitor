"""GPU 指标：通过 nvidia-smi 查询，不存在则返回空列表。"""

from __future__ import annotations

import shutil
import subprocess
from typing import Any, Dict, List, Optional


QUERY = (
    "index,uuid,name,utilization.gpu,memory.total,memory.used,"
    "temperature.gpu,power.draw,fan.speed"
)


def _to_float(value: str) -> Optional[float]:
    value = (value or "").strip()
    if not value or value.upper() == "N/A" or value == "[N/A]":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def collect_gpus(timeout: float = 8.0) -> List[Dict[str, Any]]:
    if not shutil.which("nvidia-smi"):
        return []

    cmd = [
        "nvidia-smi",
        "--query-gpu=" + QUERY,
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    gpus: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue
        idx = _to_float(parts[0])
        gpus.append(
            {
                "index": int(idx) if idx is not None else len(gpus),
                "uuid": parts[1],
                "name": parts[2],
                "util_percent": _to_float(parts[3]),
                "mem_total_mb": _to_float(parts[4]),
                "mem_used_mb": _to_float(parts[5]),
                "temp_c": _to_float(parts[6]),
                "power_w": _to_float(parts[7]),
                "fan_percent": _to_float(parts[8]) if len(parts) > 8 else None,
            }
        )
    return gpus


def collect_gpu_processes(timeout: float = 8.0) -> List[Dict[str, Any]]:
    """采集占用 GPU 的计算进程。"""
    if not shutil.which("nvidia-smi"):
        return []
    cmd = [
        "nvidia-smi",
        "--query-compute-apps=gpu_uuid,pid,process_name,used_gpu_memory",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    procs: List[Dict[str, Any]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 4:
            continue
        pid = _to_float(parts[1])
        mem = _to_float(parts[3])
        procs.append(
            {
                "gpu_uuid": parts[0],
                "pid": int(pid) if pid is not None else None,
                "process_name": parts[2],
                "used_gpu_memory_mb": mem,
            }
        )
    return procs
