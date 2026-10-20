"""GPU 指标：通过 nvidia-smi 查询，不存在则返回空列表。

策略：带 power.limit 和 pci.bus_id 的 CSV → 去掉总线 → 去掉 power.limit → 最旧的 9 列。
`--version` 在 535/470 上会失败，版本另从 `nvidia-smi -h` 抬头读取。
"""

import shutil
import subprocess
from typing import Any, Dict, List, Optional

from agent.metrics.probe import is_na, output_is_tool_error, run_cmd


QUERY = (
    "index,uuid,name,utilization.gpu,memory.total,memory.used,"
    "temperature.gpu,power.draw,fan.speed"
)
# 额定功耗：power.limit 在部分旧驱动上不可查询，采集时失败则回退 QUERY
QUERY_WITH_POWER_LIMIT = (
    "index,uuid,name,utilization.gpu,memory.total,memory.used,"
    "temperature.gpu,power.draw,power.limit,fan.speed"
)
QUERY_LIMIT_BUS = QUERY_WITH_POWER_LIMIT + ",pci.bus_id"
QUERY_BUS = QUERY + ",pci.bus_id"

_LAST_ERROR = ""


def last_error():
    # type: () -> str
    return _LAST_ERROR


def _to_float(value: str) -> Optional[float]:
    if is_na(value):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def parse_nvidia_smi_csv(
    text: str, has_power_limit: bool = False, has_bus: bool = False
) -> List[Dict[str, Any]]:
    """解析 nvidia-smi csv,noheader,nounits。

    has_power_limit 时第 9 列是 power.limit、第 10 列是风扇。
    未声明列数时，10 列仍按含 power.limit 处理（旧调用）。
    has_bus 时总线号跟在风扇后面。某一列是 N/A 或 [Not Supported] 只让该字段为空。
    """
    import csv
    import io

    gpus: List[Dict[str, Any]] = []
    reader = csv.reader(io.StringIO(text or ""))
    for raw_parts in reader:
        parts = [p.strip() for p in raw_parts]
        if len(parts) < 8:
            continue
        idx = _to_float(parts[0])
        if idx is None:
            continue
        limit_present = has_power_limit or (not has_bus and len(parts) >= 10)
        bus_id = None
        if limit_present and len(parts) >= 10:
            power_limit = _to_float(parts[8])
            fan = _to_float(parts[9]) if len(parts) > 9 else None
            if has_bus and len(parts) > 10:
                bus_id = parts[10] or None
            elif len(parts) > 10 and not has_bus:
                bus_id = parts[10] or None
        else:
            power_limit = None
            fan = _to_float(parts[8]) if len(parts) > 8 else None
            if len(parts) > 9:
                bus_id = parts[9] or None
        if bus_id and is_na(bus_id):
            bus_id = None
        gpus.append(
            {
                "index": int(idx),
                "uuid": parts[1],
                "name": parts[2] or "NVIDIA GPU",
                "model": parts[2] or "NVIDIA GPU",
                "util_percent": _to_float(parts[3]),
                "mem_total_mb": _to_float(parts[4]),
                "mem_used_mb": _to_float(parts[5]),
                "temp_c": _to_float(parts[6]),
                "power_w": _to_float(parts[7]),
                "power_limit_w": power_limit,
                "fan_percent": fan,
                "bus_id": bus_id,
            }
        )
    return gpus


def _run_nvidia_query(query: str, timeout: float) -> Optional[str]:
    global _LAST_ERROR
    result = run_cmd(
        [
            "nvidia-smi",
            "--query-gpu=" + query,
            "--format=csv,noheader,nounits",
        ],
        timeout=timeout,
    )
    if result.error:
        _LAST_ERROR = "nvidia-smi " + result.error
    if not result.text.strip() and result.error:
        return None
    return result.text


def collect_gpus(timeout: float = 8.0) -> List[Dict[str, Any]]:
    global _LAST_ERROR
    _LAST_ERROR = ""
    if not shutil.which("nvidia-smi"):
        _LAST_ERROR = "nvidia-smi 不在 PATH"
        return []

    attempts = (
        (QUERY_LIMIT_BUS, True, True),
        (QUERY_WITH_POWER_LIMIT, True, False),
        (QUERY_BUS, False, True),
        (QUERY, False, False),
    )
    saw_output = False
    for query, with_limit, with_bus in attempts:
        out = _run_nvidia_query(query, timeout)
        if not out:
            continue
        saw_output = True
        if output_is_tool_error(out) and "not a valid field" in out.lower():
            continue
        parsed = parse_nvidia_smi_csv(out, has_power_limit=with_limit, has_bus=with_bus)
        if parsed:
            _LAST_ERROR = ""
            return parsed
    if not _LAST_ERROR:
        _LAST_ERROR = "nvidia-smi 输出未能解析" if saw_output else "nvidia-smi 无输出"
    return []


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
            universal_newlines=True,
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
