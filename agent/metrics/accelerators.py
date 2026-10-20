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
import subprocess
from typing import Any, Dict, List, Match, Optional

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


def _run(cmd: List[str], timeout: float = 8.0) -> str:
    try:
        return subprocess.check_output(
            cmd,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""


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


def _parse_cnmon_info(text: str) -> List[Dict[str, Any]]:
    """尽力解析 `cnmon info` 文本（版本差异大，字段可缺省）。"""
    if not text.strip():
        return []

    cards: List[Dict[str, Any]] = []
    # 按 Card / MLU 分段
    blocks = re.split(r"(?=\bCard\s+\d+\b|\bMLU\s+\d+\b)", text, flags=re.I)
    if len(blocks) <= 1:
        blocks = [text]

    for block in blocks:
        if not re.search(r"(Card|MLU)\s+\d+", block, re.I):
            # 也可能整页无 Card 头但有 Product Name
            if "Product Name" not in block and "Board Name" not in block and "MLU" not in block.upper():
                continue
        idx_m = re.search(r"(?:Card|MLU)\s+(\d+)", block, re.I)
        name_m = re.search(
            r"(?:Product Name|Board Name|Device Name)\s*[:=]?\s*([^\n|]+)",
            block,
            re.I,
        )
        util_m = re.search(
            r"(?:MLU|Device)?\s*Utilization(?:\s*Rate)?\s*[:=]?\s*([\d.]+)\s*%?",
            block,
            re.I,
        )
        if not util_m:
            util_m = re.search(r"Utilization\s*[:=]?\s*([\d.]+)\s*%", block, re.I)
        temp_m = re.search(
            r"(?:Chip|Board|Device)?\s*Temperature\s*[:=]?\s*([\d.]+)\s*C?",
            block,
            re.I,
        )
        mem_used_m = re.search(
            r"(?:Used|Memory Used|MLU Memory Used)\s*[:=]?\s*([\d.]+)\s*(MiB|MB|GiB|GB)?",
            block,
            re.I,
        )
        mem_tot_m = re.search(
            r"(?:Total|Memory Total|MLU Memory Total)\s*[:=]?\s*([\d.]+)\s*(MiB|MB|GiB|GB)?",
            block,
            re.I,
        )
        health_m = re.search(r"Health\s*[:=]?\s*([A-Za-z]+)", block, re.I)

        # 至少要有一点卡迹象
        if not (idx_m or name_m or util_m or mem_tot_m):
            continue

        def _to_mb(val: Optional[Match]) -> Optional[float]:
            if not val:
                return None
            n = _num(val.group(1))
            if n is None:
                return None
            unit = (val.group(2) or "MiB").lower()
            if unit.startswith("g"):
                return n * 1024.0
            return n

        mu = _to_mb(mem_used_m)
        mt = _to_mb(mem_tot_m)
        mem_pct = round(mu * 100.0 / mt, 2) if mu is not None and mt and mt > 0 else None
        cards.append(
            {
                "vendor": "cambricon",
                "vendor_label": "寒武纪",
                "card_kind": "mlu",
                "index": int(idx_m.group(1)) if idx_m else len(cards),
                "name": (name_m.group(1).strip() if name_m else "Cambricon MLU"),
                "util_percent": _num(util_m.group(1)) if util_m else None,
                "mem_used_mb": mu,
                "mem_total_mb": mt,
                "mem_percent": mem_pct,
                "temp_c": _num(temp_m.group(1)) if temp_m else None,
                "health": health_m.group(1) if health_m else None,
                "power_w": None,
            }
        )
    return cards


def _from_cambricon() -> List[Dict[str, Any]]:
    if not shutil.which("cnmon"):
        # 常见安装路径
        alt = "/usr/local/neuware/bin/cnmon"
        if not os.path.isfile(alt):
            return []
        cmd_base = [alt]
    else:
        cmd_base = ["cnmon"]

    text = _run(cmd_base + ["info"], timeout=10.0)
    if not text.strip():
        # 部分版本 info 交互/失败时，再试一次短输出
        text = _run(cmd_base, timeout=6.0)
    cards = _parse_cnmon_info(text)
    if cards:
        return cards
    # 能跑通命令但解析失败：至少标记检测到驱动工具
    if text.strip() or shutil.which("cnmon") or os.path.exists("/dev/cambricon_dev0"):
        return [
            {
                "vendor": "cambricon",
                "vendor_label": "寒武纪",
                "card_kind": "mlu",
                "index": 0,
                "name": "Cambricon MLU (detected)",
                "util_percent": None,
                "mem_used_mb": None,
                "mem_total_mb": None,
                "mem_percent": None,
                "temp_c": None,
                "health": "unknown",
                "power_w": None,
                "note": "已检测到寒武纪环境，但 cnmon 文本未能解析出完整指标",
            }
        ]
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
    """探测并采集本机全部已支持厂商的加速卡。"""
    cards: List[Dict[str, Any]] = []
    cards.extend(_from_nvidia())
    cards.extend(_from_amd())
    cards.extend(_from_huawei())
    cards.extend(_from_cambricon())
    cards.extend(_from_rockchip_rknn())
    cards.extend(collect_pci_accelerators(skip_vendors=_pci_skip_vendors(cards)))
    return cards


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
