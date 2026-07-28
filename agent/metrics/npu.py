"""昇腾 NPU 指标：解析 npu-smi info（无第三方库）。"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional


def _to_float(value: str) -> Optional[float]:
    value = (value or "").strip()
    if not value or value.upper() in ("N/A", "NA", "[N/A]", "-"):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _run_npu_smi(args: List[str], timeout: float = 10.0) -> Optional[str]:
    if not shutil.which("npu-smi"):
        return None
    try:
        return subprocess.check_output(
            ["npu-smi", *args],
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _parse_info_table(text: str) -> List[Dict[str, Any]]:
    """
    解析 `npu-smi info` 成对行：
      | NPU  Name | Health | Power  Temp |
      | Chip Dev  | Bus-Id | AICore Memory-Usage |
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("|")]
    # 过滤表头/分隔
    data_lines: List[str] = []
    for ln in lines:
        body = ln.strip("|").strip()
        if not body:
            continue
        upper = body.upper()
        if upper.startswith("NPU") or upper.startswith("CHIP") or set(body) <= {"=", "+", "-", "|", " "}:
            continue
        if "NPU-SMI" in upper or "VERSION" in upper:
            continue
        data_lines.append(body)

    npus: List[Dict[str, Any]] = []
    i = 0
    while i + 1 < len(data_lines):
        top = [p for p in re.split(r"\s*\|\s*", data_lines[i]) if p != ""]
        bottom = [p for p in re.split(r"\s*\|\s*", data_lines[i + 1]) if p != ""]
        # top 列: "0  910B", "OK", "12.8  49" 或分列更多
        # 兼容字段被空白拆开的情况：整行再按空白 token 解析
        top_tokens = data_lines[i].replace("|", " ").split()
        bottom_tokens = data_lines[i + 1].replace("|", " ").split()

        # 经验：top_tokens[0]=NPU_ID, [1]=Name..., Health 为 OK/Warning/...
        health = None
        for tok in top_tokens:
            if tok.upper() in ("OK", "WARNING", "ALARM", "CRITICAL", "UNKNOWN"):
                health = tok.upper() if tok.upper() != "WARNING" else "Warning"
                if tok.upper() == "WARNING":
                    health = "Warning"
                break
        # 规范化 health 大小写
        if health:
            mapping = {
                "OK": "OK",
                "WARNING": "Warning",
                "ALARM": "Alarm",
                "CRITICAL": "Critical",
                "UNKNOWN": "UNKNOWN",
            }
            health = mapping.get(health.upper(), health)

        npu_id = _to_float(top_tokens[0]) if top_tokens else None
        # Name：NPU_ID 之后到 Health 之前
        name = ""
        if top_tokens:
            try:
                hi = next(
                    idx
                    for idx, tok in enumerate(top_tokens)
                    if tok.upper() in ("OK", "WARNING", "ALARM", "CRITICAL", "UNKNOWN")
                )
                name = " ".join(top_tokens[1:hi])
            except StopIteration:
                name = top_tokens[1] if len(top_tokens) > 1 else ""

        # Power / Temp：Health 之后的数字
        nums_after_health: List[float] = []
        seen_health = False
        for tok in top_tokens:
            if tok.upper() in ("OK", "WARNING", "ALARM", "CRITICAL", "UNKNOWN"):
                seen_health = True
                continue
            if seen_health:
                v = _to_float(tok)
                if v is not None:
                    nums_after_health.append(v)
        power_w = nums_after_health[0] if len(nums_after_health) >= 1 else None
        temp_c = nums_after_health[1] if len(nums_after_health) >= 2 else None

        # bottom: chip device bus aicore mem_used / mem_total
        aicore = None
        mem_used = None
        mem_total = None
        chip_id = _to_float(bottom_tokens[0]) if bottom_tokens else None
        bus_id = None
        for tok in bottom_tokens:
            if ":" in tok and "." in tok:
                bus_id = tok
                break
        # AICore 与 Memory：找 "数字 / 数字" 模式
        mem_match = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", data_lines[i + 1])
        if mem_match:
            mem_used = _to_float(mem_match.group(1))
            mem_total = _to_float(mem_match.group(2))
        # AICore：bus 之后、memory 之前的百分比数字
        # 简化：取 bottom 中不含 : 的纯数字，排除 chip/device/mem
        pure_nums = []
        for tok in bottom_tokens:
            if "/" in tok or ":" in tok:
                continue
            v = _to_float(tok)
            if v is not None:
                pure_nums.append(v)
        # pure_nums 通常: chip, device, aicore 或 chip, aicore
        if len(pure_nums) >= 3:
            aicore = pure_nums[2]
        elif len(pure_nums) == 2:
            # chip + aicore（Device/Bus 为 NA）
            aicore = pure_nums[1]
        elif len(pure_nums) == 1:
            aicore = pure_nums[0]

        mem_percent = None
        if mem_used is not None and mem_total and mem_total > 0:
            mem_percent = round(mem_used * 100.0 / mem_total, 2)

        npus.append(
            {
                "index": int(npu_id) if npu_id is not None else len(npus),
                "chip_id": int(chip_id) if chip_id is not None else 0,
                "name": name or "Ascend-NPU",
                "health": health or "UNKNOWN",
                "util_percent": aicore,
                "mem_used_mb": mem_used,
                "mem_total_mb": mem_total,
                "mem_percent": mem_percent,
                "temp_c": temp_c,
                "power_w": power_w,
                "bus_id": bus_id,
            }
        )
        i += 2

    return npus


def _parse_usages_text(text: str) -> Dict[str, Optional[float]]:
    result: Dict[str, Optional[float]] = {
        "util_percent": None,
        "mem_percent": None,
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key_l = key.strip().lower()
        num = _to_float(val.strip().split()[0] if val.strip() else "")
        if "aicore usage" in key_l:
            result["util_percent"] = num
        elif "memory usage rate" in key_l:
            result["mem_percent"] = num
    return result


def _enrich_with_usages(npus: List[Dict[str, Any]], timeout: float = 8.0) -> List[Dict[str, Any]]:
    for n in npus:
        need_util = n.get("util_percent") is None
        need_mem = n.get("mem_percent") is None
        if not need_util and not need_mem:
            continue
        idx = n.get("index", 0)
        chip = n.get("chip_id", 0)
        out = _run_npu_smi(
            ["info", "-t", "usages", "-i", str(idx), "-c", str(chip)],
            timeout=timeout,
        )
        if not out:
            continue
        parsed = _parse_usages_text(out)
        if need_util and parsed["util_percent"] is not None:
            n["util_percent"] = parsed["util_percent"]
        if need_mem and parsed["mem_percent"] is not None:
            n["mem_percent"] = parsed["mem_percent"]
            # 若仅有百分比，尽量保留原 used/total
    return npus


def collect_npus(timeout: float = 10.0) -> List[Dict[str, Any]]:
    out = _run_npu_smi(["info"], timeout=timeout)
    if not out:
        return []
    try:
        npus = _parse_info_table(out)
    except Exception:  # noqa: BLE001
        npus = []
    if not npus:
        return []
    try:
        return _enrich_with_usages(npus, timeout=min(8.0, timeout))
    except Exception:  # noqa: BLE001
        return npus
