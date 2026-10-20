"""加速卡统一采集：按本机可用命令自动识别厂商并适配。

当前支持：
- 英伟达 NVIDIA：`nvidia-smi`
- AMD：优先 `rocm-smi`，失败回退 `amd-smi`
- 华为昇腾 Huawei Ascend：`npu-smi`
- 寒武纪 Cambricon MLU：`cnmon`
- 瑞芯微 Rockchip RKNN 系列：`/sys/kernel/debug/rknpu/load` 等
- 没有厂商工具和设备节点时，从 `/sys/bus/pci/devices` 识别已知加速卡（不依赖 lspci）

返回统一字段，便于中心端阈值判定与展示。
"""

import os
import re
import shutil
from typing import Any, Dict, List, Optional

from agent.metrics.amd import collect_amd_gpus
from agent.metrics.gpu import collect_gpu_processes, collect_gpus
from agent.metrics.npu import collect_npus

_GPU_VENDORS = ("nvidia", "amd")


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _run(cmd: List[str], timeout: float = 8.0, cwd: Optional[str] = None) -> str:
    """跑命令。stderr 并进 stdout；非零退出和超时仍把已有输出交回解析。"""
    global _LAST_CMD
    from agent.metrics.probe import run_cmd

    _LAST_CMD = run_cmd(cmd, timeout=timeout, cwd=cwd)
    return _LAST_CMD.text


_LAST_CMD = None
_CAMBRICON_FAILURE = ""


def _from_nvidia() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for g in collect_gpus():
        item = dict(g)
        item["vendor"] = "nvidia"
        item["vendor_label"] = "英伟达"
        item["card_kind"] = "gpu"
        mu = _num(item.get("mem_used_mb"))
        mt = _num(item.get("mem_total_mb"))
        if mu is not None and mt and mt > 0:
            item["mem_percent"] = round(mu * 100.0 / mt, 2)
        out.append(item)
    return out


def _from_amd() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for g in collect_amd_gpus():
        item = dict(g)
        item["vendor"] = "amd"
        item["vendor_label"] = "AMD"
        item["card_kind"] = "gpu"
        mu = _num(item.get("mem_used_mb"))
        mt = _num(item.get("mem_total_mb"))
        if mu is not None and mt and mt > 0:
            item["mem_percent"] = round(mu * 100.0 / mt, 2)
        out.append(item)
    return out


def _from_huawei() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for n in collect_npus():
        item = dict(n)
        item["vendor"] = "huawei"
        item["vendor_label"] = "华为昇腾"
        item["card_kind"] = "npu"
        out.append(item)
    return out


_CARD_HEADER = re.compile(r"(?m)^Card\s+(\d+)\s*$")
_KV_LINE = re.compile(r"^(\s*)(.+?)\s*:\s*(.*)$")
_FIRST_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_MEM_VALUE = re.compile(r"([\d.]+)\s*(KiB|KB|MiB|MB|GiB|GB|B)?", re.I)
_CLUSTER_KEY = re.compile(r"^Cluster\s+\d+$", re.I)


def _first_num(text):
    # type: (Any) -> Optional[float]
    if text is None:
        return None
    match = _FIRST_NUM.search(str(text))
    if not match:
        return None
    return _num(match.group(0))


def _mem_to_mb(text):
    # type: (Any) -> Optional[float]
    """Physical Memory 的 Total/Used。无单位时按 MiB。"""
    if text is None:
        return None
    match = _MEM_VALUE.search(str(text))
    if not match:
        return None
    amount = _num(match.group(1))
    if amount is None:
        return None
    unit = (match.group(2) or "MiB").lower()
    if unit in ("kb", "kib"):
        return amount / 1024.0
    if unit in ("gb", "gib"):
        return amount * 1024.0
    if unit == "b":
        return amount / (1024.0 * 1024.0)
    return amount


def _cnmon_sections(block):
    # type: (str) -> tuple
    """按缩进把一张卡拆成顶层字段和节。空值的行是节头，子行归入该节。"""
    scalars = {}  # type: Dict[str, str]
    sections = {}  # type: Dict[str, Dict[str, str]]
    current = None  # type: Optional[str]
    base_indent = None  # type: Optional[int]
    for raw in block.splitlines():
        line = raw.expandtabs(4).rstrip()
        if not line.strip():
            continue
        match = _KV_LINE.match(line)
        if not match:
            continue
        indent = len(match.group(1))
        key = match.group(2).strip()
        value = match.group(3).strip()
        if not key:
            continue
        if base_indent is None or indent <= base_indent:
            if base_indent is None:
                base_indent = indent
            current = None
            if value:
                scalars[key] = value
            else:
                current = key
                sections.setdefault(key, {})
            continue
        if current:
            sections[current][key] = value
    return scalars, sections


def _cnmon_temp_c(section):
    # type: (Dict[str, str]) -> Optional[float]
    chip = _first_num(section.get("Chip"))
    if chip is not None:
        return chip
    board = _first_num(section.get("Board"))
    if board is not None:
        return board
    clusters = []  # type: List[float]
    for key, value in section.items():
        if not _CLUSTER_KEY.match(key):
            continue
        reading = _first_num(value)
        if reading is not None:
            clusters.append(reading)
    if not clusters:
        return None
    return max(clusters)


def _parse_cnmon_card(block, index):
    # type: (str, int) -> Dict[str, Any]
    scalars, sections = _cnmon_sections(block)
    name = (
        scalars.get("Product Name")
        or scalars.get("Board Name")
        or scalars.get("Device Name")
        or "Cambricon MLU"
    )
    util = _first_num((sections.get("Utilization") or {}).get("MLU Average"))
    temp = _cnmon_temp_c(sections.get("Temperature") or {})
    physical = sections.get("Physical Memory Usage") or {}
    used = _mem_to_mb(physical.get("Used"))
    total = _mem_to_mb(physical.get("Total"))
    mem_pct = None
    if used is not None and total is not None and total > 0:
        mem_pct = round(used * 100.0 / total, 2)
    health = (sections.get("Health State") or {}).get("Device") or None
    if health is not None:
        health = health.strip() or None
    power = _first_num((sections.get("Power") or {}).get("Usage"))
    pci = sections.get("PCI") or {}
    bus_id = _bdf_from_cnmon_parts(
        pci.get("Domain ID"),
        pci.get("Bus num"),
        pci.get("Device"),
        pci.get("Function"),
    )
    model = name.strip() or "Cambricon MLU"
    return {
        "vendor": "cambricon",
        "vendor_label": "寒武纪",
        "card_kind": "mlu",
        "index": index,
        "name": model,
        "model": model,
        "bus_id": bus_id,
        "util_percent": util,
        "mem_used_mb": used,
        "mem_total_mb": total,
        "mem_percent": mem_pct,
        "temp_c": temp,
        "health": health,
        "power_w": power,
        "source": "cnmon",
    }


def _parse_cnmon_info(text: str) -> List[Dict[str, Any]]:
    """解析 `cnmon info` 文本。只按整行 `Card N` 分卡，不把核簇 `MLU 0-3` 当成新卡。

    利用率、温度、健康、显存、功耗都在节头下面的子行里。cnmon 5.10（MLU370）
    和 1.19（MLU270）缩进结构相同；MLU270 没有 Health State，健康留空。
    """
    if not text or not text.strip():
        return []
    headers = list(_CARD_HEADER.finditer(text))
    if not headers:
        return []
    cards = []  # type: List[Dict[str, Any]]
    for pos, header in enumerate(headers):
        start = header.end()
        end = headers[pos + 1].start() if pos + 1 < len(headers) else len(text)
        cards.append(_parse_cnmon_card(text[start:end], int(header.group(1))))
    version = _cnmon_banner_version(text)
    for card in cards:
        if version and not card.get("tool_version"):
            card["tool_version"] = version
    return cards


def _bdf_from_cnmon_parts(domain, bus, device, function):
    # type: (Any, Any, Any, Any) -> Optional[str]
    def _hx(value):
        if value is None:
            return None
        text = str(value).strip()
        if not text or text.upper() in ("N/A", "NA"):
            return None
        return int(text, 16)

    try:
        dom = _hx(domain)
        bus_n = _hx(bus)
        dev_n = _hx(device)
        fn = _hx(function)
    except (TypeError, ValueError):
        return None
    if None in (dom, bus_n, dev_n, fn):
        return None
    return "%04x:%02x:%02x.%x" % (dom, bus_n, dev_n, fn)


def _cnmon_banner_version(text):
    # type: (str) -> Optional[str]
    match = re.search(r"CNMON\s+v?([0-9]+(?:\.[0-9]+)+)", text or "", re.I)
    if not match:
        return None
    return match.group(1)


def _json_num(value):
    # type: (Any) -> Optional[float]
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return _first_num(value)


def _json_temp(temp):
    # type: (Any) -> Optional[float]
    if not isinstance(temp, dict):
        return None
    chip = _json_num(temp.get("Chip"))
    if chip is not None:
        return chip
    board = _json_num(temp.get("Board"))
    if board is not None:
        return board
    clusters = temp.get("Cluster")
    readings = []  # type: List[float]
    if isinstance(clusters, list):
        for item in clusters:
            reading = _json_num(item)
            if reading is not None:
                readings.append(reading)
    else:
        reading = _json_num(clusters)
        if reading is not None:
            readings.append(reading)
    if not readings:
        return None
    return max(readings)


def parse_cnmon_json(text: str) -> List[Dict[str, Any]]:
    """解析 `cnmon info -j`。物理显存、MLUAverage、Chip/Board/Cluster、Power.Usage。"""
    import json

    raw = (text or "").strip()
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    info = data.get("CnmonInfo")
    if isinstance(info, dict):
        info = [info]
    if not isinstance(info, list):
        return []
    version = data.get("CnmonVersion")
    if version is not None:
        version = str(version).strip() or None
    cards = []  # type: List[Dict[str, Any]]
    for item in info:
        if not isinstance(item, dict):
            continue
        cards.append(_card_from_cnmon_json(item, version))
    return cards


def _card_from_cnmon_json(item, version):
    # type: (Dict[str, Any], Optional[str]) -> Dict[str, Any]
    try:
        index = int(item.get("CardNum"))
    except (TypeError, ValueError):
        index = 0
    name = str(item.get("ProductName") or "").strip() or "Cambricon MLU"
    util = None
    temp = None
    used = None
    total = None
    health = None
    power = None
    bus_id = None
    try:
        util_block = item.get("Utilization") or {}
        if isinstance(util_block, dict):
            util = _json_num(util_block.get("MLUAverage"))
    except (TypeError, ValueError):
        util = None
    try:
        temp = _json_temp(item.get("Temperature"))
    except (TypeError, ValueError):
        temp = None
    try:
        physical = item.get("PhysicalMemUsage") or {}
        if isinstance(physical, dict):
            used = _json_num(physical.get("Used"))
            total = _json_num(physical.get("Total"))
    except (TypeError, ValueError):
        used = None
        total = None
    try:
        health_block = item.get("HealthState") or {}
        if isinstance(health_block, dict):
            raw_health = health_block.get("Device")
            if isinstance(raw_health, str) and raw_health.strip():
                health = raw_health.strip()
    except (TypeError, ValueError):
        health = None
    try:
        power_block = item.get("Power") or {}
        if isinstance(power_block, dict):
            power = _json_num(power_block.get("Usage"))
    except (TypeError, ValueError):
        power = None
    try:
        pci = item.get("PCI") or {}
        if isinstance(pci, dict):
            bus_id = _bdf_from_cnmon_parts(
                pci.get("DomainID") or pci.get("Domain ID"),
                pci.get("Bus") or pci.get("Bus num"),
                pci.get("Device"),
                pci.get("Function"),
            )
    except (TypeError, ValueError):
        bus_id = None
    mem_pct = None
    if used is not None and total is not None and total > 0:
        mem_pct = round(used * 100.0 / total, 2)
    return {
        "vendor": "cambricon",
        "vendor_label": "寒武纪",
        "card_kind": "mlu",
        "index": index,
        "name": name,
        "model": name,
        "bus_id": bus_id,
        "util_percent": util,
        "mem_used_mb": used,
        "mem_total_mb": total,
        "mem_percent": mem_pct,
        "temp_c": temp,
        "health": health,
        "power_w": power,
        "tool_version": version,
        "source": "cnmon-json",
    }


_CNMON_TABLE_HEAD = re.compile(r"^\|\s*(\d+)\s+\S+\s+(MLU\S+)")
_CNMON_MEM_PAIR = re.compile(r"(\d+(?:\.\d+)?)\s*MiB\s*/\s*(\d+(?:\.\d+)?)\s*MiB", re.I)
_CNMON_BDF = re.compile(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]")


def _parse_cnmon_table(text: str) -> List[Dict[str, Any]]:
    """解析 `cnmon` 摘要表。info 文本失败时的最后一层文本策略。"""
    lines = [line.rstrip() for line in (text or "").splitlines()]
    cards = []  # type: List[Dict[str, Any]]
    for pos, line in enumerate(lines):
        match = _CNMON_TABLE_HEAD.match(line.strip())
        if not match:
            continue
        stats = lines[pos + 1] if pos + 1 < len(lines) else ""
        if not stats.strip().startswith("|"):
            continue
        name = match.group(2)
        index = int(match.group(1))
        util = None
        util_match = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
        if util_match:
            util = _num(util_match.group(1))
        temp = None
        temp_match = re.search(r"(\d+(?:\.\d+)?)\s*C", stats, re.I)
        if temp_match:
            temp = _num(temp_match.group(1))
        power = None
        power_match = re.search(r"(\d+(?:\.\d+)?)\s*W\s*/", stats, re.I)
        if power_match:
            power = _num(power_match.group(1))
        used = None
        total = None
        pairs = []
        for mem in _CNMON_MEM_PAIR.finditer(stats):
            pairs.append((_num(mem.group(1)), _num(mem.group(2))))
        chosen = None
        for pair in pairs:
            if pair[1] is None:
                continue
            if chosen is None or pair[1] < chosen[1]:
                chosen = pair
        if chosen:
            used, total = chosen
        bus_match = _CNMON_BDF.search(line)
        mem_pct = None
        if used is not None and total is not None and total > 0:
            mem_pct = round(used * 100.0 / total, 2)
        cards.append(
            {
                "vendor": "cambricon",
                "vendor_label": "寒武纪",
                "card_kind": "mlu",
                "index": index,
                "name": name,
                "model": name,
                "bus_id": bus_match.group(0).lower() if bus_match else None,
                "util_percent": util,
                "mem_used_mb": used,
                "mem_total_mb": total,
                "mem_percent": mem_pct,
                "temp_c": temp,
                "health": None,
                "power_w": power,
                "tool_version": _cnmon_banner_version(text),
                "source": "cnmon-table",
            }
        )
    return cards


def _cnmon_bin():
    # type: () -> Optional[List[str]]
    if shutil.which("cnmon"):
        return ["cnmon"]
    alt = "/usr/local/neuware/bin/cnmon"
    if os.path.isfile(alt):
        return [alt]
    return None


def _read_cnmon_json(cmd, timeout):
    # type: (List[str], float) -> str
    """`cnmon info -j` 会在 cwd 写下 cnmon_info.json，所以只在临时目录里跑。"""
    import tempfile

    tmp = tempfile.mkdtemp(prefix="cnmon-json-")
    try:
        text = _run(cmd + ["info", "-j"], timeout=timeout, cwd=tmp)
        path = os.path.join(tmp, "cnmon_info.json")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                    file_text = fh.read()
            except OSError:
                file_text = ""
            if file_text.strip():
                text = file_text
        return text
    finally:
        shutil.rmtree(tmp, True)


def cambricon_failure():
    # type: () -> str
    return _CAMBRICON_FAILURE


def _from_cambricon() -> List[Dict[str, Any]]:
    """JSON（临时目录）→ info 文本 → 摘要表。失败原因留给 PCI 卡的 note。"""
    global _CAMBRICON_FAILURE
    global _LAST_CMD
    _CAMBRICON_FAILURE = ""
    cmd = _cnmon_bin()
    if not cmd:
        return []
    errors = []  # type: List[str]
    _LAST_CMD = None
    json_text = _read_cnmon_json(cmd, 10.0)
    cards = parse_cnmon_json(json_text)
    if cards:
        return cards
    if _LAST_CMD is not None and _LAST_CMD.error:
        errors.append("cnmon info -j " + _LAST_CMD.error)
    _LAST_CMD = None
    info_text = _run(cmd + ["info"], timeout=10.0)
    cards = _parse_cnmon_info(info_text)
    if cards:
        return cards
    if _LAST_CMD is not None and _LAST_CMD.error:
        errors.append("cnmon info " + _LAST_CMD.error)
    elif (info_text or "").strip():
        errors.append("cnmon info 文本未能解析")
    _LAST_CMD = None
    table_text = _run(cmd, timeout=6.0)
    cards = _parse_cnmon_table(table_text)
    if cards:
        return cards
    if _LAST_CMD is not None and _LAST_CMD.error:
        errors.append("cnmon " + _LAST_CMD.error)
    elif (table_text or "").strip() and not errors:
        errors.append("cnmon 输出未能解析")
    _CAMBRICON_FAILURE = "；".join(errors)[:400]
    return []


def _read_first(paths: List[str]) -> Optional[str]:
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
        except OSError:
            continue
    return None


def parse_rknpu_load(text: str) -> Dict[str, Any]:
    """解析 RKNN / rknpu 负载文本。

    示例（多核 RK3588）：
      NPU load:  Core0:  21%, Core1:  11%, Core2:  0%,
    示例（单核）：
      NPU load: 35%
    """
    cores: List[Dict[str, Any]] = []
    for m in re.finditer(r"Core\s*(\d+)\s*:\s*([\d.]+)\s*%", text or "", re.I):
        cores.append(
            {
                "index": int(m.group(1)),
                "util_percent": _num(m.group(2)),
            }
        )
    util: Optional[float] = None
    if cores:
        vals = [c["util_percent"] for c in cores if c.get("util_percent") is not None]
        if vals:
            util = round(sum(vals) / len(vals), 2)
    else:
        m = re.search(r"(?:NPU\s*load\s*:)?\s*([\d.]+)\s*%", text or "", re.I)
        if m:
            util = _num(m.group(1))
    return {"util_percent": util, "cores": cores, "core_count": len(cores)}


def _rknn_freq_hz(kind: str = "cur") -> Optional[float]:
    fname = "max_freq" if kind == "max" else "cur_freq"
    txt = _read_first(
        [
            "/sys/class/devfreq/fdab0000.npu/%s" % fname,
            "/sys/devices/platform/fde40000.npu/devfreq/fde40000.npu/%s" % fname,
            "/sys/class/devfreq/fde40000.npu/%s" % fname,
        ]
    )
    return _num(txt) if txt else None


def _rknn_temp_c() -> Optional[float]:
    """尽力从 thermal_zone 名称含 npu 的节点读温度（毫度→℃）。"""
    base = "/sys/class/thermal"
    if not os.path.isdir(base):
        return None
    try:
        for name in sorted(os.listdir(base)):
            if not name.startswith("thermal_zone"):
                continue
            tdir = os.path.join(base, name)
            typ = _read_first([os.path.join(tdir, "type")]) or ""
            if "npu" not in typ.lower():
                continue
            raw = _read_first([os.path.join(tdir, "temp")])
            val = _num(raw)
            if val is None:
                continue
            # 常见为毫摄氏度
            return round(val / 1000.0, 1) if val > 200 else val
    except OSError:
        return None
    return None


def _from_rockchip_rknn() -> List[Dict[str, Any]]:
    """瑞芯微 RKNN 系列 NPU（rknpu 驱动 + debugfs load）。"""
    markers = [
        "/dev/rknpu",
        "/dev/rknpu0",
        "/sys/module/rknpu",
        "/sys/kernel/debug/rknpu",
        "/sys/kernel/debug/rknpu/load",
        "/sys/devices/platform/fde40000.npu",
        "/sys/class/devfreq/fdab0000.npu",
    ]
    found = any(os.path.exists(p) for p in markers)
    if not found:
        try:
            mods = open("/proc/modules", "r", encoding="utf-8", errors="ignore").read()
            if re.search(r"\brknpu\b", mods):
                found = True
        except OSError:
            pass
    # 用户态 RKNN 库也可作旁证（无驱动时不单独报卡）
    has_rknn_lib = any(
        os.path.exists(p)
        for p in (
            "/usr/lib/librknnrt.so",
            "/usr/lib/aarch64-linux-gnu/librknnrt.so",
            "/usr/lib/librknn_api.so",
        )
    )
    if not found and not has_rknn_lib:
        return []
    if not found:
        return []

    version = _read_first(
        [
            "/sys/kernel/debug/rknpu/version",
            "/sys/module/rknpu/version",
        ]
    )
    load_txt = _read_first(
        [
            "/sys/kernel/debug/rknpu/load",
            "/sys/class/devfreq/fdab0000.npu/load",
            "/sys/devices/platform/fde40000.npu/devfreq/fde40000.npu/load",
        ]
    )
    parsed = parse_rknpu_load(load_txt or "")
    freq_hz = _rknn_freq_hz("cur")
    freq_max_hz = _rknn_freq_hz("max")
    name = "Rockchip RKNN NPU"
    if version:
        name = "Rockchip RKNN (%s)" % version.splitlines()[0].strip()[:48]

    item: Dict[str, Any] = {
        "vendor": "rockchip",
        "vendor_label": "瑞芯微 RKNN",
        "card_kind": "npu",
        "series": "rknn",
        "index": 0,
        "name": name,
        "util_percent": parsed.get("util_percent"),
        "core_count": parsed.get("core_count") or None,
        "cores": parsed.get("cores") or [],
        "mem_used_mb": None,
        "mem_total_mb": None,
        "mem_percent": None,
        "temp_c": _rknn_temp_c(),
        "freq_hz": freq_hz,
        "freq_mhz": round(freq_hz / 1e6, 1) if freq_hz else None,
        "freq_max_hz": freq_max_hz,
        "freq_max_mhz": round(freq_max_hz / 1e6, 1) if freq_max_hz else None,
        "health": "OK",
        "power_w": None,
        "driver": "rknpu",
    }
    return [item]


# (vendor_id, device_id) -> (vendor, vendor_label, card_kind, name)
_PCI_DEVICE_NAMES = {
    ("cabc", "0270"): ("cambricon", "寒武纪", "mlu", "MLU270"),
    ("cabc", "0290"): ("cambricon", "寒武纪", "mlu", "MLU290"),
    ("cabc", "0370"): ("cambricon", "寒武纪", "mlu", "MLU370"),
    ("cabc", "0580"): ("cambricon", "寒武纪", "mlu", "MLU580"),
    ("10de", "2204"): ("nvidia", "英伟达", "gpu", "GeForce RTX 3090"),
    ("19e5", "d100"): ("huawei", "华为昇腾", "npu", "Ascend 310"),
}

_PCI_VENDOR_META = {
    "cambricon": ("寒武纪", "mlu", "Cambricon MLU"),
    "nvidia": ("英伟达", "gpu", "NVIDIA GPU"),
    "amd": ("AMD", "gpu", "AMD GPU"),
    "huawei": ("华为昇腾", "npu", "Huawei Ascend"),
}


def _pci_hex(text: Optional[str]) -> str:
    raw = (text or "").strip().lower()
    if raw.startswith("0x"):
        raw = raw[2:]
    return raw


def _pci_class_code(text: Optional[str]) -> str:
    """sysfs class 是 24 位，例如 0x120000。取高字节 12 / 03。"""
    raw = _pci_hex(text)
    if len(raw) < 2:
        return ""
    if len(raw) >= 6:
        return raw[:2]
    return raw[:2]


def _pci_read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except OSError:
        return ""


def pci_sysfs_root() -> str:
    return os.environ.get("MONITOR_SYSFS_PCI") or "/sys/bus/pci/devices"


def _classify_pci(vendor: str, class_code: str) -> str:
    """返回内部厂商键；不认识则空串。寒武纪只做加速卡，class 读失败也保留。"""
    if vendor == "cabc":
        return "cambricon"
    if vendor == "10de" and class_code in ("03", "12"):
        return "nvidia"
    if vendor == "1002" and class_code == "03":
        return "amd"
    if vendor == "19e5" and class_code == "12":
        return "huawei"
    return ""


def _pci_note(revision: str) -> str:
    if revision == "ff":
        return "设备无响应 (rev ff)"
    return "驱动/管理工具缺失"


def _pci_card(bdf: str, vendor_id: str, device_id: str, revision: str, vendor_key: str) -> Dict[str, Any]:
    named = _PCI_DEVICE_NAMES.get((vendor_id, device_id))
    if named:
        _key, label, kind, name = named
    else:
        label, kind, fallback = _PCI_VENDOR_META[vendor_key]
        name = "%s (%s:%s)" % (fallback, vendor_id, device_id)
    return {
        "vendor": vendor_key,
        "vendor_label": label,
        "card_kind": kind,
        "index": 0,
        "name": name,
        "pci": "%s:%s" % (vendor_id, device_id),
        "bdf": bdf,
        "util_percent": None,
        "mem_used_mb": None,
        "mem_total_mb": None,
        "mem_percent": None,
        "temp_c": None,
        "health": "abnormal",
        "power_w": None,
        "note": _pci_note(revision),
        "bus_id": bdf,
        "model": name,
        "source": "pci",
    }


def collect_pci_accelerators(
    sysfs_root: Optional[str] = None,
    skip_vendors: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """扫描 sysfs PCI 设备。skip_vendors 里的厂商已经由工具报过，不再重复。"""
    root = sysfs_root or pci_sysfs_root()
    if not os.path.isdir(root):
        return []
    skip = set(v.lower() for v in (skip_vendors or []))
    found: List[Dict[str, Any]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    for name in names:
        slot = os.path.join(root, name)
        if not os.path.isdir(slot):
            continue
        vendor_id = _pci_hex(_pci_read(os.path.join(slot, "vendor")))
        device_id = _pci_hex(_pci_read(os.path.join(slot, "device")))
        class_code = _pci_class_code(_pci_read(os.path.join(slot, "class")))
        revision = _pci_hex(_pci_read(os.path.join(slot, "revision")))
        if not vendor_id or not device_id:
            continue
        vendor_key = _classify_pci(vendor_id, class_code)
        if not vendor_key or vendor_key in skip:
            continue
        found.append(_pci_card(name, vendor_id, device_id, revision, vendor_key))
    for idx, card in enumerate(found):
        card["index"] = idx
    return found


def _vendor_tool_vendors() -> List[str]:
    """厂商工具或设备节点还在时，不要再用 PCI 补一张卡。"""
    covered = []
    if (
        shutil.which("nvidia-smi")
        or os.path.exists("/dev/nvidiactl")
        or os.path.exists("/dev/nvidia0")
    ):
        covered.append("nvidia")
    if shutil.which("rocm-smi") or shutil.which("amd-smi") or os.path.exists("/dev/kfd"):
        covered.append("amd")
    if (
        shutil.which("npu-smi")
        or os.path.exists("/dev/davinci0")
        or os.path.exists("/dev/davinci_manager")
    ):
        covered.append("huawei")
    if (
        shutil.which("cnmon")
        or os.path.isfile("/usr/local/neuware/bin/cnmon")
        or os.path.exists("/dev/cambricon_dev0")
        or os.path.exists("/dev/cambricon_ctl")
    ):
        covered.append("cambricon")
    return covered


def _pci_skip_vendors(found: List[Dict[str, Any]]) -> List[str]:
    skip = list(_vendor_tool_vendors())
    for card in found:
        vendor = (card.get("vendor") or "").lower()
        if vendor and vendor not in skip:
            skip.append(vendor)
    return skip


def collect_accelerators() -> List[Dict[str, Any]]:
    """各厂商按「结构化输出 → 文本 → PCI」采集，再按总线号合并。"""
    global _CAMBRICON_FAILURE
    from agent.metrics.probe import merge_tool_and_pci, normalize_card
    from agent.metrics import amd as amd_mod
    from agent.metrics import gpu as gpu_mod
    from agent.metrics import npu as npu_mod

    _CAMBRICON_FAILURE = ""
    gpu_mod._LAST_ERROR = ""
    amd_mod._LAST_ERROR = ""
    npu_mod._LAST_ERROR = ""
    nvidia = _from_nvidia()
    amd = _from_amd()
    huawei = _from_huawei()
    cambricon = _from_cambricon()
    rockchip = _from_rockchip_rknn()
    tool = [normalize_card(card) for card in nvidia + amd + huawei + cambricon + rockchip]
    failures = {}  # type: Dict[str, str]
    if not nvidia and gpu_mod.last_error():
        failures["nvidia"] = gpu_mod.last_error()
    if not amd and amd_mod.last_error():
        failures["amd"] = amd_mod.last_error()
    if not huawei and npu_mod.last_error():
        failures["huawei"] = npu_mod.last_error()
    if not cambricon and cambricon_failure():
        failures["cambricon"] = cambricon_failure()
    present = set(v.lower() for v in _vendor_tool_vendors())
    have = set((card.get("vendor") or "").lower() for card in tool)
    skip = []
    for vendor in present:
        if vendor not in have and vendor not in failures:
            skip.append(vendor)
    pci = collect_pci_accelerators(skip_vendors=skip)
    return merge_tool_and_pci(tool, pci, failures)


def split_for_payload(cards: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """兼容旧字段：gpus=NVIDIA/AMD GPU；npus=其余加速卡（华为/寒武纪/瑞芯微 RKNN 等）。"""
    gpus: List[Dict[str, Any]] = []
    npus: List[Dict[str, Any]] = []
    for card in cards:
        vendor = (card.get("vendor") or "").lower()
        kind = (card.get("card_kind") or "").lower()
        if vendor in _GPU_VENDORS or kind == "gpu":
            gpus.append(card)
        else:
            npus.append(card)
    return {"gpus": gpus, "npus": npus, "accelerators": cards}


def collect_nvidia_processes() -> List[Dict[str, Any]]:
    return collect_gpu_processes()
