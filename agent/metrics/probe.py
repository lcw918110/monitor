"""加速卡采集的公共探针：命令结果、总线号、字段归一和工具/PCI 去重。

各厂商自己决定命令顺序（结构化输出 → 文本 → 失败原因）。
本模块只保证：一个坏字段不会丢掉整张卡，总线号能对齐，PCI 回退不重复计数。
"""

import re
import subprocess
from typing import Any, Dict, List, Optional

_NA = ("", "N/A", "NA", "[N/A]", "[NOT SUPPORTED]", "NOT SUPPORTED", "-", "NONE", "NULL")
_BDF_RE = re.compile(
    r"([0-9a-fA-F]{4,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])"
)
_NVIDIA_HELP_VER = re.compile(
    r"NVIDIA System Management Interface -- v(\d+\.\d+(?:\.\d+)?)"
)
_NVIDIA_DRIVER_VER = re.compile(
    r"DRIVER version\s*:\s*(\d+\.\d+(?:\.\d+)?)", re.I
)

_VENDOR_STUB = {
    "nvidia": ("英伟达", "gpu", "NVIDIA GPU"),
    "amd": ("AMD", "gpu", "AMD GPU"),
    "huawei": ("华为昇腾", "npu", "Huawei Ascend"),
    "cambricon": ("寒武纪", "mlu", "Cambricon MLU"),
    "rockchip": ("瑞芯微 RKNN", "npu", "Rockchip RKNN"),
}


class CmdResult(object):
    """一次外部命令的结果。error 为空表示退出码 0。"""

    def __init__(self, argv, returncode, text, error):
        # type: (List[str], Optional[int], str, str) -> None
        self.argv = argv
        self.returncode = returncode
        self.text = text or ""
        self.error = error or ""

    @property
    def ok(self):
        # type: () -> bool
        return self.returncode == 0 and not self.error


def _as_text(data):
    # type: (Any) -> str
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


def _clip(text, limit=180):
    # type: (str, int) -> str
    line = ""
    for raw in (text or "").splitlines():
        raw = raw.strip()
        if raw:
            line = raw
            break
    if len(line) > limit:
        return line[:limit]
    return line


def run_cmd(cmd, timeout=8.0, cwd=None):
    # type: (List[str], float, Optional[str]) -> CmdResult
    """跑命令。stdout 和 stderr 都保留；非零退出、超时、无法执行都带回原因。"""
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            cwd=cwd,
        )
    except subprocess.TimeoutExpired as exc:
        text = _as_text(getattr(exc, "output", None))
        err = _as_text(getattr(exc, "stderr", None))
        body = text if not err else (text + "\n" + err)
        return CmdResult(list(cmd), None, body, "超时 %.0fs" % timeout)
    except OSError as exc:
        return CmdResult(list(cmd), None, "", "无法执行 %s (%s)" % (cmd[0], exc))
    text = proc.stdout or ""
    err = proc.stderr or ""
    body = text if not err else (text + ("\n" if text and not text.endswith("\n") else "") + err)
    if proc.returncode != 0:
        reason = "exit %s" % proc.returncode
        detail = _clip(err or text)
        if detail:
            reason = "%s: %s" % (reason, detail)
        return CmdResult(list(cmd), proc.returncode, body, reason)
    return CmdResult(list(cmd), 0, body, "")


def is_na(value):
    # type: (Any) -> bool
    if value is None:
        return True
    return str(value).strip().upper() in _NA


def first_present(*values):
    # type: (Any) -> Any
    """跳过 None。0 和 0.0 是有效读数，不能当成缺失。"""
    for value in values:
        if value is not None:
            return value
    return None


def norm_bdf(value):
    # type: (Any) -> str
    """把 `00000001:6F:00.0` / `0000:86:00.0` 收成 `0001:6f:00.0`。对不上则空串。"""
    if value is None:
        return ""
    match = _BDF_RE.search(str(value).strip())
    if not match:
        return ""
    try:
        domain = int(match.group(1), 16)
        bus = int(match.group(2), 16)
        dev = int(match.group(3), 16)
        fn = int(match.group(4), 16)
    except ValueError:
        return ""
    return "%04x:%02x:%02x.%x" % (domain, bus, dev, fn)


def parse_nvidia_tool_version(help_text, version_text=""):
    # type: (str, str) -> Optional[str]
    """版本优先看 `nvidia-smi -h` 抬头。535/470 的 `--version` 会直接报错。"""
    match = _NVIDIA_HELP_VER.search(help_text or "")
    if match:
        return match.group(1)
    match = _NVIDIA_DRIVER_VER.search(version_text or "")
    if match:
        return match.group(1)
    match = _NVIDIA_DRIVER_VER.search(help_text or "")
    if match:
        return match.group(1)
    return None


def output_is_tool_error(text):
    # type: (str) -> bool
    low = (text or "").lower()
    needles = (
        "invalid card id",
        "does not support",
        "error parameter",
        "not recognized",
        "invalid combination",
        "not a valid field",
        "command not found",
        "no such file",
    )
    return any(needle in low for needle in needles)


def normalize_card(card):
    # type: (Dict[str, Any]) -> Dict[str, Any]
    """补齐统一字段。单个字段异常时留空，不丢掉这张卡。"""
    item = dict(card or {})
    name = item.get("name") or item.get("model") or ""
    if name and not item.get("model"):
        item["model"] = name
    if item.get("model") and not item.get("name"):
        item["name"] = item["model"]
    bdf = norm_bdf(item.get("bus_id") or item.get("bdf"))
    if bdf:
        item["bus_id"] = bdf
    for key in (
        "util_percent",
        "mem_used_mb",
        "mem_total_mb",
        "mem_percent",
        "temp_c",
        "power_w",
        "health",
        "note",
    ):
        if key not in item:
            item[key] = None
    used = item.get("mem_used_mb")
    total = item.get("mem_total_mb")
    if item.get("mem_percent") is None and used is not None and total:
        try:
            total_f = float(total)
            if total_f > 0:
                item["mem_percent"] = round(float(used) * 100.0 / total_f, 2)
        except (TypeError, ValueError):
            pass
    return item


def _stub_card(vendor, note):
    # type: (str, str) -> Dict[str, Any]
    label, kind, name = _VENDOR_STUB.get(vendor, (vendor, "accel", vendor))
    return normalize_card(
        {
            "vendor": vendor,
            "vendor_label": label,
            "card_kind": kind,
            "index": 0,
            "name": name,
            "model": name,
            "health": None,
            "note": note,
            "source": "tool-error",
        }
    )


def merge_tool_and_pci(tool_cards, pci_cards, failures=None):
    # type: (List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, str]]) -> List[Dict[str, Any]]
    """工具卡优先。有总线号就按总线去重；没有总线号时同一厂商不再加 PCI 卡。

    工具失败且 PCI 还能看见卡时，把失败原因写进 note。
    工具没安装（「不在 PATH」）时不另造一张空卡。
    """
    fails = failures or {}
    tool = [normalize_card(card) for card in tool_cards or []]
    pci = [normalize_card(card) for card in pci_cards or []]
    covered = set()
    for card in tool:
        vendor = (card.get("vendor") or "").lower()
        if vendor and not norm_bdf(card.get("bus_id")):
            covered.add(vendor)
    matched = set()
    for card in tool:
        bdf = norm_bdf(card.get("bus_id"))
        if not bdf:
            continue
        for index, other in enumerate(pci):
            if norm_bdf(other.get("bus_id") or other.get("bdf")) == bdf:
                matched.add(index)
    out = list(tool)
    seen = set((card.get("vendor") or "").lower() for card in out)
    for index, card in enumerate(pci):
        if index in matched:
            continue
        vendor = (card.get("vendor") or "").lower()
        if vendor in covered:
            continue
        fail = fails.get(vendor) or ""
        if fail:
            note = card.get("note") or ""
            if fail not in note:
                card["note"] = (note + "；" + fail).strip("；")
        out.append(card)
        seen.add(vendor)
    for vendor, fail in fails.items():
        if not fail or vendor in seen:
            continue
        if "不在 PATH" in fail or "未安装" in fail:
            continue
        out.append(_stub_card(vendor, fail))
    return out
