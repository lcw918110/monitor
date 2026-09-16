"""昇腾 NPU 指标：解析 npu-smi info（无第三方库）。"""

from __future__ import annotations

import re
import shutil
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple


_HEALTH_TOKENS = ("OK", "WARNING", "ALARM", "CRITICAL", "UNKNOWN")
_HEALTH_MAP = {
    "OK": "OK",
    "WARNING": "Warning",
    "ALARM": "Alarm",
    "CRITICAL": "Critical",
    "UNKNOWN": "UNKNOWN",
}
_HEADER_FIELD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_/-]*(?:\([^)]+\))?")
# 进程表/MCU 不能当成加速卡
_MCU_NAME_RE = re.compile(r"\bmcu\b", re.I)


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


def _norm_field(name: str) -> str:
    name = (name or "").strip().lower()
    name = re.sub(r"\([^)]*\)", "", name)
    return name.strip(" -_")


def _pipe_cells(line: str) -> List[str]:
    raw = line.strip()
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|"):
        raw = raw[:-1]
    return [c.strip() for c in raw.split("|")]


def _header_fields_from_cells(cells: Iterable[str]) -> List[str]:
    fields: List[str] = []
    for cell in cells:
        fields.extend(_HEADER_FIELD_RE.findall(cell))
    return fields


def _is_rule_line(line: str) -> bool:
    body = line.strip().replace("|", "").replace("+", "").replace(" ", "")
    return bool(body) and set(body) <= {"=", "-"}


def _is_banner_line(line: str) -> bool:
    upper = line.upper()
    return "NPU-SMI" in upper and "VERSION" in upper


def _is_process_header(line: str) -> bool:
    return "PROCESS" in line.upper()


def _is_device_header(line: str) -> bool:
    upper = line.upper()
    if "PROCESS" in upper:
        return False
    return "NPU" in upper and "HEALTH" in upper


def _is_chip_header(line: str) -> bool:
    upper = line.upper()
    if "PROCESS" in upper:
        return False
    return "CHIP" in upper and ("AICORE" in upper or "BUS-ID" in upper or "MEMORY" in upper)


def _line_has_health(line: str) -> bool:
    tokens = line.replace("|", " ").split()
    return any(tok.upper() in _HEALTH_TOKENS for tok in tokens)


def _group_value_tokens(cell: str) -> List[str]:
    """把 `380 / 380`、`2962 / 21527` 收成一个值，避免 Hugepages 把 Temp 挤偏。"""
    raw = cell.split()
    out: List[str] = []
    i = 0
    while i < len(raw):
        if (
            i + 2 < len(raw)
            and raw[i + 1] == "/"
            and _to_float(raw[i]) is not None
            and _to_float(raw[i + 2]) is not None
        ):
            out.append("%s / %s" % (raw[i], raw[i + 2]))
            i += 3
            continue
        out.append(raw[i])
        i += 1
    return out


def _parse_used_total(value: str) -> Tuple[Optional[float], Optional[float]]:
    m = re.search(r"(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)", value or "")
    if not m:
        return None, None
    return _to_float(m.group(1)), _to_float(m.group(2))


def _normalize_health(token: Optional[str]) -> Optional[str]:
    if not token:
        return None
    return _HEALTH_MAP.get(token.strip().upper())


def _is_mcu_name(name: str) -> bool:
    return bool(_MCU_NAME_RE.search(name or ""))


def _util_valid(value: Any) -> bool:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 <= num <= 100.0


def _temp_valid(value: Any) -> bool:
    """合理芯片温度；Hugepages 页数（如 380）不应落入此区间后仍当温度。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False
    return -40.0 <= num <= 200.0


def _assign_id_name_cell(cell: str, fields: List[str], into: Dict[str, Any]) -> None:
    parts = cell.split()
    if not parts:
        return
    ident = _to_float(parts[0])
    rest = " ".join(parts[1:])
    for field in fields:
        key = _norm_field(field)
        if key == "npu":
            into["index"] = int(ident) if ident is not None else into.get("index")
        elif key == "chip":
            into["chip_id"] = int(ident) if ident is not None else 0
        elif key == "name":
            into["name"] = rest or into.get("name") or "Ascend-NPU"
        elif key == "device":
            into["device"] = _to_float(rest.split()[0]) if rest else None


def _assign_metric_fields(fields: List[str], values: List[str], into: Dict[str, Any]) -> None:
    for field, raw in zip(fields, values):
        key = _norm_field(field)
        if key == "health":
            health = _normalize_health(raw)
            if health:
                into["health"] = health
        elif key in ("bus-id", "bus_id"):
            into["bus_id"] = None if raw.upper() in ("NA", "N/A", "-") else raw
        elif key == "power":
            into["power_w"] = _to_float(raw)
        elif key == "temp":
            into["temp_c"] = _to_float(raw)
        elif key == "aicore":
            into["util_percent"] = _to_float(raw)
        elif key in ("memory-usage", "memory"):
            used, total = _parse_used_total(raw)
            into["mem_used_mb"] = used
            into["mem_total_mb"] = total
        # Hugepages-Usage 等有意忽略，避免当成温度/功耗


def _top_row_values_after_health(top_line: str) -> List[str]:
    cells = _pipe_cells(top_line)
    values: List[str] = []
    for cell in cells[1:]:
        values.extend(_group_value_tokens(cell))
    if values and values[0].upper() in _HEALTH_TOKENS:
        values = values[1:]
    return values


def _realign_power_temp_hugepages(top_line: str, card: Dict[str, Any]) -> None:
    """按列顺序重读顶行：Power(W)、Temp(C)、可选 Hugepages-Usage(page)。

    24.x 三列挤在同一格。Power=NA 时不能把后面的 Temp 当成功耗、把 Hugepages
    页数当成温度（线上曾出现 power_w=71、temp_c=380）。
    """
    values = _top_row_values_after_health(top_line)
    if not values:
        return
    if "/" in values[-1]:
        values = values[:-1]
    if not values:
        return
    if len(values) == 1:
        card["power_w"] = None
        card["temp_c"] = _to_float(values[0])
        return
    card["power_w"] = _to_float(values[0])
    card["temp_c"] = _to_float(values[1])


def _parse_device_pair(
    top_line: str,
    bottom_line: str,
    top_fields: List[str],
    bottom_fields: List[str],
) -> Optional[Dict[str, Any]]:
    if not _line_has_health(top_line):
        return None

    top_cells = _pipe_cells(top_line)
    bottom_cells = _pipe_cells(bottom_line)
    card: Dict[str, Any] = {
        "index": 0,
        "chip_id": 0,
        "name": "Ascend-NPU",
        "health": "UNKNOWN",
        "util_percent": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "mem_percent": None,
        "temp_c": None,
        "power_w": None,
        "bus_id": None,
    }

    if top_fields:
        # 第一格固定为 NPU + Name
        if top_cells:
            npu_fields = [f for f in top_fields if _norm_field(f) in ("npu", "name")]
            _assign_id_name_cell(top_cells[0], npu_fields or ["NPU", "Name"], card)
        metric_fields = [f for f in top_fields if _norm_field(f) not in ("npu", "name")]
        metric_values: List[str] = []
        for cell in top_cells[1:]:
            metric_values.extend(_group_value_tokens(cell))
        _assign_metric_fields(metric_fields, metric_values, card)

    if bottom_fields:
        if bottom_cells:
            chip_fields = [f for f in bottom_fields if _norm_field(f) in ("chip", "device")]
            _assign_id_name_cell(bottom_cells[0], chip_fields or ["Chip", "Device"], card)
        metric_fields = [f for f in bottom_fields if _norm_field(f) not in ("chip", "device")]
        metric_values = []
        for cell in bottom_cells[1:]:
            metric_values.extend(_group_value_tokens(cell))
        _assign_metric_fields(metric_fields, metric_values, card)

    if _is_mcu_name(str(card.get("name") or "")):
        return None

    _realign_power_temp_hugepages(top_line, card)

    mem_used = card.get("mem_used_mb")
    mem_total = card.get("mem_total_mb")
    if mem_used is not None and mem_total and mem_total > 0:
        card["mem_percent"] = round(float(mem_used) * 100.0 / float(mem_total), 2)
    return card


def _parse_info_table(text: str) -> List[Dict[str, Any]]:
    """
    解析 `npu-smi info` 设备表成对行，忽略进程表。

    顶行：NPU / Name / Health / Power / Temp /（可选）Hugepages-Usage
    底行：Chip / Device / Bus-Id / AICore / Memory-Usage
    """
    top_fields: List[str] = []
    bottom_fields: List[str] = []
    pending_top: Optional[str] = None
    reading_process = False
    npus: List[Dict[str, Any]] = []

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if _is_rule_line(stripped) or _is_banner_line(stripped):
            continue
        if not stripped.startswith("|"):
            continue

        if _is_process_header(stripped):
            reading_process = True
            pending_top = None
            continue
        if _is_device_header(stripped):
            reading_process = False
            top_fields = _header_fields_from_cells(_pipe_cells(stripped))
            bottom_fields = []
            pending_top = None
            continue
        if reading_process:
            continue
        if _is_chip_header(stripped):
            bottom_fields = _header_fields_from_cells(_pipe_cells(stripped))
            pending_top = None
            continue

        if not top_fields:
            continue
        if pending_top is None:
            if _line_has_health(stripped):
                pending_top = stripped
            continue
        card = _parse_device_pair(pending_top, stripped, top_fields, bottom_fields)
        pending_top = None
        if card is not None:
            npus.append(card)

    return _dedup_cards(npus)


def _parse_chip_mapping(text: str) -> List[Dict[str, Any]]:
    """解析 `npu-smi info -m`。MCU 行会标 is_mcu=True，不能计入加速卡。"""
    chips: List[Dict[str, Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper().startswith("NPU ID"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        npu_id = _to_float(parts[0])
        chip_id = _to_float(parts[1])
        if npu_id is None or chip_id is None:
            continue
        logic_id = parts[2]
        name = " ".join(parts[3:])
        chips.append(
            {
                "npu_id": int(npu_id),
                "chip_id": int(chip_id),
                "logic_id": None if logic_id in ("-",) else logic_id,
                "name": name,
                "is_mcu": _is_mcu_name(name),
            }
        )
    return chips


def _compute_chips(chips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [c for c in chips if not c.get("is_mcu")]


def _dedup_cards(npus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """同一物理卡只保留第一条（进程表误配对时的兜底）。"""
    seen = set()
    out: List[Dict[str, Any]] = []
    for card in npus:
        key = (card.get("index"), card.get("chip_id", 0))
        if key in seen:
            continue
        seen.add(key)
        out.append(card)
    return out


def _apply_chip_mapping(
    npus: List[Dict[str, Any]], chips: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    compute = _compute_chips(chips)
    if not compute:
        return npus
    valid_ids = {c["npu_id"] for c in compute}
    names = {c["npu_id"]: c["name"] for c in compute}
    chip_ids = {}
    for chip in compute:
        chip_ids.setdefault(chip["npu_id"], chip["chip_id"])
    filtered = [n for n in npus if n.get("index") in valid_ids]
    for card in filtered:
        idx = card.get("index")
        if not card.get("name") or card.get("name") == "Ascend-NPU":
            mapped = names.get(idx)
            if mapped:
                card["name"] = mapped
        # typed 查询必须打到计算芯片，不能用 MCU 的 Chip ID
        if idx in chip_ids:
            card["chip_id"] = chip_ids[idx]
    return filtered or npus


def _cards_from_mapping(chips: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    cards: List[Dict[str, Any]] = []
    seen = set()
    for chip in _compute_chips(chips):
        npu_id = chip["npu_id"]
        if npu_id in seen:
            continue
        seen.add(npu_id)
        cards.append(
            {
                "index": npu_id,
                "chip_id": chip.get("chip_id", 0),
                "name": chip.get("name") or "Ascend-NPU",
                "health": "UNKNOWN",
                "util_percent": None,
                "mem_used_mb": None,
                "mem_total_mb": None,
                "mem_percent": None,
                "temp_c": None,
                "power_w": None,
                "bus_id": None,
            }
        )
    return cards


def _parse_usages_text(text: str) -> Dict[str, Optional[float]]:
    result: Dict[str, Optional[float]] = {
        "util_percent": None,
        "mem_percent": None,
        "mem_total_mb": None,
    }
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key_l = key.strip().lower()
        token = val.strip().split()[0] if val.strip() else ""
        num = _to_float(token)
        if "aicore usage" in key_l:
            result["util_percent"] = num
        elif "hugepages" in key_l:
            continue
        elif "ddr usage rate" in key_l or "memory usage rate" in key_l:
            result["mem_percent"] = num
        elif "ddr capacity" in key_l:
            result["mem_total_mb"] = num
    return result


def _parse_temp_text(text: str) -> Optional[float]:
    board: Optional[float] = None
    aicore: Optional[float] = None
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        key_l = key.strip().lower()
        token = val.strip().split()[0] if val.strip() else ""
        num = _to_float(token)
        if num is None:
            continue
        if "aicore" in key_l.replace(" ", ""):
            aicore = num
        elif "temperature" in key_l:
            board = num
    value = board if board is not None else aicore
    return value if _temp_valid(value) else None


def _run_typed_query(
    kind: str, npu_id: int, chip_id: int, timeout: float
) -> Optional[str]:
    out = _run_npu_smi(
        ["info", "-t", kind, "-i", str(npu_id), "-c", str(chip_id)],
        timeout=timeout,
    )
    if out and out.strip():
        return out
    return _run_npu_smi(["info", "-t", kind, "-i", str(npu_id)], timeout=timeout)


def _sanitize_parsed_metrics(npus: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    for card in npus:
        if not _util_valid(card.get("util_percent")):
            card["util_percent"] = None
        if not _temp_valid(card.get("temp_c")):
            card["temp_c"] = None
    return npus


def _apply_typed_temp(card: Dict[str, Any], typed_temp: Optional[float]) -> None:
    """用 `npu-smi info -t temp` 覆盖温度，并清掉 Power/Temp 对调残留。"""
    if typed_temp is None or not _temp_valid(typed_temp):
        return
    table_temp = card.get("temp_c")
    table_power = card.get("power_w")
    # 线上错位：power_w=71（实为 Temp）、temp_c=380（实为 Hugepages）
    if table_power is not None and table_power == typed_temp and table_temp != typed_temp:
        card["power_w"] = None
    card["temp_c"] = typed_temp


def _enrich_with_typed_queries(
    npus: List[Dict[str, Any]], timeout: float = 8.0
) -> List[Dict[str, Any]]:
    for card in npus:
        idx = int(card.get("index") or 0)
        chip = int(card.get("chip_id") or 0)
        need_util = not _util_valid(card.get("util_percent"))
        need_mem = card.get("mem_percent") is None
        # 温度始终走 -t temp：表头 Power/Temp/Hugepages 共格时最容易错位
        temp_out = _run_typed_query("temp", idx, chip, timeout)
        if temp_out:
            _apply_typed_temp(card, _parse_temp_text(temp_out))
        if need_util or need_mem:
            out = _run_typed_query("usages", idx, chip, timeout)
            if out:
                parsed = _parse_usages_text(out)
                if need_util and _util_valid(parsed.get("util_percent")):
                    card["util_percent"] = parsed["util_percent"]
                if need_mem and parsed.get("mem_percent") is not None:
                    card["mem_percent"] = parsed["mem_percent"]
                if card.get("mem_total_mb") is None and parsed.get("mem_total_mb") is not None:
                    card["mem_total_mb"] = parsed["mem_total_mb"]
    return npus


def collect_npus(timeout: float = 10.0) -> List[Dict[str, Any]]:
    out = _run_npu_smi(["info"], timeout=timeout)
    npus: List[Dict[str, Any]] = []
    if out:
        try:
            npus = _parse_info_table(out)
        except Exception:  # noqa: BLE001
            npus = []

    mapping_timeout = min(8.0, timeout)
    mapping_out = _run_npu_smi(["info", "-m"], timeout=mapping_timeout)
    chips: List[Dict[str, Any]] = []
    if mapping_out:
        try:
            chips = _parse_chip_mapping(mapping_out)
        except Exception:  # noqa: BLE001
            chips = []
    if chips:
        if npus:
            npus = _apply_chip_mapping(npus, chips)
        else:
            npus = _cards_from_mapping(chips)

    npus = _dedup_cards(npus)
    npus = _sanitize_parsed_metrics(npus)
    if not npus:
        return []
    try:
        return _enrich_with_typed_queries(npus, timeout=mapping_timeout)
    except Exception:  # noqa: BLE001
        return npus
