"""AMD GPU 指标：优先 `rocm-smi`，失败再回退 `amd-smi`。无工具则空列表。"""

import csv
import io
import json
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple


_CARD_KEY_RE = re.compile(r"^card(\d+)$", re.I)
_GPU_LINE_RE = re.compile(
    r"^GPU\[(\d+)\]\s*:\s*(.+?)\s*[:=]\s*(.+?)\s*$", re.I
)


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.upper() in ("N/A", "NA", "[N/A]", "-", "NONE"):
        return None
    text = text.replace(",", "")
    m = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


def _run(cmd: List[str], timeout: float) -> str:
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


def _loads_json(text: str) -> Any:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        pass
    start_obj = raw.find("{")
    start_arr = raw.find("[")
    starts = [i for i in (start_obj, start_arr) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end_obj = raw.rfind("}")
    end_arr = raw.rfind("]")
    end = max(end_obj, end_arr)
    if end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except ValueError:
        return None


def _unwrap(value: Any) -> Any:
    """amd-smi 常见 `{value, unit}` 包装。"""
    if isinstance(value, dict):
        if "value" in value:
            return value.get("value")
        if len(value) == 1:
            return _unwrap(next(iter(value.values())))
    return value


def _pick(obj: Any, keys: List[str]) -> Any:
    if not isinstance(obj, dict):
        return None
    lower = {}
    for k, v in obj.items():
        lower[str(k).strip().lower()] = v
    for key in keys:
        if key in obj:
            return obj[key]
        found = lower.get(key.strip().lower())
        if found is not None:
            return found
    for key in keys:
        want = key.strip().lower()
        for dk, dv in obj.items():
            if str(dk).strip().lower() == want:
                return dv
    return None


def _bytes_to_mb(value: Any) -> Optional[float]:
    n = _to_float(value)
    if n is None:
        return None
    # amd-smi 的 VRAM 常已是 MB；rocm-smi JSON 是字节
    if n >= 1024.0 * 1024.0:
        return round(n / (1024.0 * 1024.0), 2)
    return n


def _mem_percent(used: Optional[float], total: Optional[float]) -> Optional[float]:
    if used is None or total is None or total <= 0:
        return None
    return round(used * 100.0 / total, 2)


def _name_from_rocm(obj: Dict[str, Any]) -> str:
    for key in (
        "Card series",
        "Card Series",
        "Device Name",
        "Card model",
        "Card Model",
        "Card SKU",
    ):
        val = obj.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text and text.upper() not in ("N/A", "NA", "-"):
            return text
    return "AMD GPU"


def _temp_from_rocm(obj: Dict[str, Any]) -> Optional[float]:
    preferred = (
        "Temperature (Sensor edge) (C)",
        "Temperature (Sensor junction) (C)",
        "Temperature (Sensor hotspot) (C)",
        "Temperature (Sensor mem) (C)",
    )
    for key in preferred:
        val = _to_float(obj.get(key))
        if val is not None:
            return val
    for key, val in obj.items():
        low = str(key).lower()
        if "temp" in low and "sensor" in low:
            num = _to_float(val)
            if num is not None:
                return num
    return _to_float(_pick(obj, ["Temperature (C)", "Temp"]))


def _power_from_rocm(obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    power = None
    for key in (
        "Average Graphics Package Power (W)",
        "Current Socket Graphics Package Power (W)",
        "Current Graphics Package Power (W)",
    ):
        power = _to_float(obj.get(key))
        if power is not None:
            break
    limit = _to_float(
        _pick(
            obj,
            [
                "Max Graphics Package Power (W)",
                "Power Cap (W)",
                "Max Graphics Package Power",
            ],
        )
    )
    return power, limit


def _vram_from_rocm(obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    total = _bytes_to_mb(
        _pick(
            obj,
            [
                "VRAM Total Memory (B)",
                "vram total memory (B)",
                "VRAM Total Memory",
            ],
        )
    )
    used = _bytes_to_mb(
        _pick(
            obj,
            [
                "VRAM Total Used Memory (B)",
                "VRAM Used Memory (B)",
                "vram total used memory (B)",
                "vram used memory (B)",
            ],
        )
    )
    return used, total


def _index_from_card_key(key: str, fallback: int) -> int:
    m = _CARD_KEY_RE.match(str(key or ""))
    if m:
        return int(m.group(1))
    return fallback


def _card_from_rocm_obj(key: str, obj: Any, fallback_index: int = 0) -> Optional[Dict[str, Any]]:
    if not isinstance(obj, dict):
        return None
    used, total = _vram_from_rocm(obj)
    util = _to_float(
        _pick(obj, ["GPU use (%)", "GPU Use (%)", "gpu use (%)", "GPU use"])
    )
    power_w, power_limit = _power_from_rocm(obj)
    name = _name_from_rocm(obj)
    # 至少要有利用率、显存或产品名之一
    if util is None and total is None and name == "AMD GPU":
        if not any("card" in str(k).lower() or "gpu" in str(k).lower() for k in obj.keys()):
            return None
    idx = _index_from_card_key(key, fallback_index)
    fan = _to_float(_pick(obj, ["Fan Speed (%)", "Fan speed (%)", "Fan Speed (Level)"]))
    return {
        "vendor": "amd",
        "vendor_label": "AMD",
        "card_kind": "gpu",
        "index": idx,
        "name": name,
        "util_percent": util,
        "mem_used_mb": used,
        "mem_total_mb": total,
        "mem_percent": _mem_percent(used, total),
        "temp_c": _temp_from_rocm(obj),
        "power_w": power_w,
        "power_limit_w": power_limit,
        "fan_percent": fan,
        "health": None,
        "source": "rocm-smi",
    }


def _iter_rocm_card_items(data: Any) -> List[Tuple[str, Any]]:
    if isinstance(data, dict):
        items = [(k, v) for k, v in data.items() if _CARD_KEY_RE.match(str(k))]
        if items:
            items.sort(key=lambda kv: _index_from_card_key(kv[0], 0))
            return items
        for wrap in ("body", "devices", "gpu_data", "card"):
            if wrap in data:
                nested = _iter_rocm_card_items(data[wrap])
                if nested:
                    return nested
        # 单卡对象：自身含 GPU use / VRAM
        if _pick(data, ["GPU use (%)", "VRAM Total Memory (B)", "Card Series"]):
            return [("card0", data)]
    if isinstance(data, list):
        out: List[Tuple[str, Any]] = []
        for i, item in enumerate(data):
            out.append(("card%d" % i, item))
        return out
    return []


def parse_rocm_smi_json(text: str) -> List[Dict[str, Any]]:
    data = _loads_json(text)
    if data is None:
        return []
    cards: List[Dict[str, Any]] = []
    for i, (key, obj) in enumerate(_iter_rocm_card_items(data)):
        card = _card_from_rocm_obj(key, obj, fallback_index=i)
        if card:
            cards.append(card)
    return cards


def parse_rocm_smi_csv(text: str) -> List[Dict[str, Any]]:
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        reader = csv.DictReader(io.StringIO(raw))
    except Exception:  # noqa: BLE001
        return []
    cards: List[Dict[str, Any]] = []
    for row in reader:
        if not row:
            continue
        device = str(row.get("device") or row.get("Device") or "").strip()
        if device and not _CARD_KEY_RE.match(device) and not device.lower().startswith("card"):
            if _to_float(device) is None:
                continue
        card = _card_from_rocm_obj(device or ("card%d" % len(cards)), row, len(cards))
        if card:
            cards.append(card)
    return cards


def parse_rocm_smi_text(text: str) -> List[Dict[str, Any]]:
    """解析 `GPU[0] : Field: value` 多行文本。"""
    grouped: Dict[int, Dict[str, str]] = {}
    for raw in (text or "").splitlines():
        m = _GPU_LINE_RE.match(raw.strip())
        if not m:
            continue
        idx = int(m.group(1))
        field = m.group(2).strip()
        val = m.group(3).strip()
        grouped.setdefault(idx, {})[field] = val
    cards: List[Dict[str, Any]] = []
    for idx in sorted(grouped):
        card = _card_from_rocm_obj("card%d" % idx, grouped[idx], idx)
        if card:
            cards.append(card)
    return cards


def _metric_list(data: Any) -> List[Dict[str, Any]]:
    if data is None:
        return []
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if not isinstance(data, dict):
        return []
    for key in ("gpu_data", "gpu_metrics", "devices", "card", "gpus", "metrics"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            nested = _metric_list(val)
            if nested:
                return nested
    # 单卡
    if any(k in data for k in ("gpu", "asic", "usage", "mem_usage", "temperature")):
        return [data]
    return []


def _dig(obj: Any, path: List[str]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        if key in cur:
            cur = cur[key]
            continue
        found = None
        want = key.lower()
        for dk, dv in cur.items():
            if str(dk).lower() == want:
                found = dv
                break
        if found is None:
            return None
        cur = found
    return _unwrap(cur)


def _amd_smi_index(obj: Dict[str, Any], fallback: int) -> int:
    for key in ("gpu", "index", "gpu_id", "id"):
        num = _to_float(obj.get(key))
        if num is not None:
            return int(num)
    return fallback


def _amd_smi_name(static_obj: Optional[Dict[str, Any]], metric_obj: Dict[str, Any]) -> str:
    sources = [static_obj or {}, metric_obj]
    paths = (
        ["asic", "market_name"],
        ["asic", "vendor_name"],
        ["market_name"],
        ["product_name"],
        ["card_series"],
        ["name"],
    )
    for src in sources:
        for path in paths:
            val = _dig(src, path)
            text = str(val).strip() if val is not None else ""
            if text and text.upper() not in ("N/A", "NA", "-"):
                if path != ["asic", "vendor_name"]:
                    return text
    return "AMD GPU"


def _amd_smi_mem(obj: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    used = _to_float(
        _dig(obj, ["mem_usage", "used_vram"])
        or _dig(obj, ["vram", "used"])
        or _dig(obj, ["memory", "used_vram"])
    )
    total = _to_float(
        _dig(obj, ["mem_usage", "total_vram"])
        or _dig(obj, ["vram", "total"])
        or _dig(obj, ["memory", "total_vram"])
    )
    # 若像字节（远大于 1GiB 以 MB 计）
    if total is not None and total >= 1024.0 * 1024.0:
        total = round(total / (1024.0 * 1024.0), 2)
        if used is not None:
            used = round(used / (1024.0 * 1024.0), 2)
    return used, total


def parse_amd_smi_json(
    metric_text: str, static_text: Optional[str] = None
) -> List[Dict[str, Any]]:
    metric_data = _loads_json(metric_text)
    static_data = _loads_json(static_text or "")
    metrics = _metric_list(metric_data)
    statics = _metric_list(static_data)
    static_by_idx: Dict[int, Dict[str, Any]] = {}
    for i, item in enumerate(statics):
        static_by_idx[_amd_smi_index(item, i)] = item
    if not metrics and statics:
        metrics = statics
    cards: List[Dict[str, Any]] = []
    for i, obj in enumerate(metrics):
        idx = _amd_smi_index(obj, i)
        static_obj = static_by_idx.get(idx)
        used, total = _amd_smi_mem(obj)
        if static_obj:
            s_used, s_total = _amd_smi_mem(static_obj)
            if used is None:
                used = s_used
            if total is None:
                total = s_total
        util = _to_float(
            _dig(obj, ["usage", "gfx_activity"])
            or _dig(obj, ["average_gfx_activity"])
            or _dig(obj, ["gfx_activity"])
            or _dig(obj, ["gpu_activity"])
            or _dig(obj, ["usage", "gpu_activity"])
        )
        temp = _to_float(
            _dig(obj, ["temperature", "edge"])
            or _dig(obj, ["temperature", "hotspot"])
            or _dig(obj, ["temperature", "junction"])
            or _dig(obj, ["edge_temperature"])
        )
        power = _to_float(
            _dig(obj, ["power", "socket_power"])
            or _dig(obj, ["power", "average_socket_power"])
            or _dig(obj, ["socket_power"])
        )
        limit = _to_float(
            _dig(obj, ["power", "power_limit"])
            or _dig(obj, ["power", "slowdown_power"])
            or _dig(obj, ["power_cap"])
        )
        name = _amd_smi_name(static_obj, obj)
        if util is None and used is None and total is None and name == "AMD GPU":
            continue
        cards.append(
            {
                "vendor": "amd",
                "vendor_label": "AMD",
                "card_kind": "gpu",
                "index": idx,
                "name": name,
                "util_percent": util,
                "mem_used_mb": used,
                "mem_total_mb": total,
                "mem_percent": _mem_percent(used, total),
                "temp_c": temp,
                "power_w": power,
                "power_limit_w": limit,
                "fan_percent": _to_float(_dig(obj, ["fan", "speed"])),
                "health": None,
                "source": "amd-smi",
            }
        )
    cards.sort(key=lambda c: int(c.get("index") or 0))
    return cards


_ROCM_JSON_CMD = [
    "rocm-smi",
    "--showuse",
    "--showmeminfo",
    "vram",
    "--showtemp",
    "--showproductname",
    "--showpower",
    "--json",
]
_ROCM_CSV_CMD = [
    "rocm-smi",
    "--showuse",
    "--showmeminfo",
    "vram",
    "--showtemp",
    "--showproductname",
    "--showpower",
    "--csv",
]


def _collect_rocm_smi(timeout: float) -> List[Dict[str, Any]]:
    if not shutil.which("rocm-smi"):
        return []
    out = _run(_ROCM_JSON_CMD, timeout)
    cards = parse_rocm_smi_json(out)
    if cards:
        return cards
    out = _run(_ROCM_CSV_CMD, timeout)
    cards = parse_rocm_smi_csv(out)
    if cards:
        return cards
    # 无 --json/--csv 的旧版：分项查询拼 GPU[n] 文本
    text_out = _run(
        [
            "rocm-smi",
            "--showuse",
            "--showmeminfo",
            "vram",
            "--showtemp",
            "--showproductname",
            "--showpower",
        ],
        timeout,
    )
    return parse_rocm_smi_text(text_out)


def _collect_amd_smi(timeout: float) -> List[Dict[str, Any]]:
    if not shutil.which("amd-smi"):
        return []
    metric = _run(["amd-smi", "metric", "--json"], timeout)
    static = _run(["amd-smi", "static", "--json"], timeout)
    cards = parse_amd_smi_json(metric, static)
    if cards:
        return cards
    report = _run(["amd-smi", "report", "--json"], timeout)
    return parse_amd_smi_json(report, static)


def collect_amd_gpus(timeout: float = 8.0) -> List[Dict[str, Any]]:
    """优先 rocm-smi；无卡或命令失败时回退 amd-smi。"""
    cards = _collect_rocm_smi(timeout)
    if cards:
        return cards
    return _collect_amd_smi(timeout)
