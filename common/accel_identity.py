"""加速卡库存摘要：厂商 + 精简型号 + 数量。供列表/时段统计展示。"""

import re
from collections import OrderedDict
from typing import Any, Dict, Iterable, List, Optional, Tuple


VENDOR_SUMMARY_LABEL = {
    "nvidia": "NVIDIA",
    "amd": "AMD",
    "huawei": "Ascend",
    "cambricon": "Cambricon",
    "rockchip": "RKNN",
}

_VENDOR_PREFIX_RE = re.compile(
    r"^(?:NVIDIA|AMD(?:/ATI)?|Advanced Micro Devices(?:,?\s*Inc\.)?"
    r"(?:\s*\[AMD(?:/ATI)?\])?)\s+",
    re.I,
)
_GEFORCE_RE = re.compile(r"^GeForce\s+", re.I)


def _text(value: Any) -> str:
    return str(value or "").strip()


def cards_from_payload(payload: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """优先 accelerators；旧 Agent 回退 npus+gpus。"""
    if not isinstance(payload, dict):
        return []
    acc = payload.get("accelerators")
    if isinstance(acc, list) and acc:
        return [dict(c) for c in acc if isinstance(c, dict)]
    cards: List[Dict[str, Any]] = []
    for n in payload.get("npus") or []:
        if not isinstance(n, dict):
            continue
        item = dict(n)
        item.setdefault("vendor", "huawei")
        item.setdefault("vendor_label", "加速卡")
        cards.append(item)
    for g in payload.get("gpus") or []:
        if not isinstance(g, dict):
            continue
        item = dict(g)
        item.setdefault("vendor", "nvidia")
        item.setdefault("vendor_label", "英伟达")
        cards.append(item)
    return cards


def vendor_key(card: Dict[str, Any]) -> str:
    raw = _text(card.get("vendor")).lower()
    if raw in VENDOR_SUMMARY_LABEL:
        return raw
    label = _text(card.get("vendor_label")).lower()
    if "nvidia" in raw or "英伟达" in label or "nvidia" in label:
        return "nvidia"
    if raw == "amd" or "amd" in label:
        return "amd"
    if "huawei" in raw or "昇腾" in label or "ascend" in label:
        return "huawei"
    if "cambricon" in raw or "寒武纪" in label:
        return "cambricon"
    if "rockchip" in raw or "rknn" in label or "瑞芯" in label:
        return "rockchip"
    name = _text(card.get("name")).lower()
    if "nvidia" in name or "geforce" in name or "rtx " in name:
        return "nvidia"
    if "radeon" in name or "navi" in name:
        return "amd"
    if "ascend" in name or "310p" in name:
        return "huawei"
    return raw or "other"


def vendor_summary_label(key: str, card: Optional[Dict[str, Any]] = None) -> str:
    if key in VENDOR_SUMMARY_LABEL:
        return VENDOR_SUMMARY_LABEL[key]
    if card:
        label = _text(card.get("vendor_label"))
        if label:
            return label
        raw = _text(card.get("vendor"))
        if raw:
            return raw.upper()
    return (key or "GPU").upper()


def _collapse_rx_family(name: str) -> str:
    """Navi 括号内 `Radeon RX 6800/6800 XT / 6900 XT` → `Radeon RX 6800/6900`。"""
    m = re.match(r"^(.*?\b(?:Radeon\s+)?RX)\s+(.+)$", name, re.I)
    if not m:
        return name
    prefix = re.sub(r"\s+", " ", m.group(1)).strip()
    rest = m.group(2)
    nums: List[str] = []
    for part in re.split(r"\s*/\s*", rest):
        nm = re.search(r"(\d{3,4})", part)
        if not nm:
            continue
        num = nm.group(1)
        if num not in nums:
            nums.append(num)
    if len(nums) >= 2:
        return "%s %s" % (prefix, "/".join(nums))
    return name


def shorten_card_model(name: Any) -> str:
    raw = _text(name)
    if not raw or raw.upper() in ("N/A", "NA", "-"):
        return ""
    bracket = re.search(r"\[([^\]]+)\]", raw)
    if bracket:
        inner = bracket.group(1).strip()
        if re.search(
            r"(Radeon|GeForce|RX\s|RTX\s|Tesla|Instinct|Quadro|A\d{2,3}|H\d{2,3})",
            inner,
            re.I,
        ):
            raw = inner
    raw = _VENDOR_PREFIX_RE.sub("", raw)
    raw = _GEFORCE_RE.sub("", raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    raw = _collapse_rx_family(raw)
    return raw


def _model_of(card: Dict[str, Any]) -> str:
    short = shorten_card_model(card.get("name"))
    if short:
        return short
    sku = _text(card.get("sku") or card.get("card_sku"))
    if sku:
        return sku
    kind = _text(card.get("card_kind")).upper() or "GPU"
    return kind


def summarize_accelerators(cards: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """返回 count / summary / inventory。

    摘要形如：`AMD ×5 Radeon RX 6800/6900`、`NVIDIA ×2 RTX 3090`。
    """
    groups: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    count = 0
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        count += 1
        vkey = vendor_key(card)
        model = _model_of(card)
        gkey = (vkey, model)
        if gkey not in groups:
            groups[gkey] = {
                "vendor": vkey,
                "vendor_label": vendor_summary_label(vkey, card),
                "model": model,
                "name": _text(card.get("name")) or model,
                "count": 0,
            }
        groups[gkey]["count"] += 1

    inventory = list(groups.values())
    vendor_order: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for item in inventory:
        vendor_order.setdefault(item["vendor"], []).append(item)

    parts: List[str] = []
    for vkey, items in vendor_order.items():
        label = items[0]["vendor_label"]
        total = sum(int(x["count"]) for x in items)
        if len(items) == 1:
            model = items[0]["model"]
            if model:
                parts.append("%s ×%d %s" % (label, total, model))
            else:
                parts.append("%s ×%d" % (label, total))
            continue
        bits = []
        for item in items:
            bits.append("%s ×%d" % (item["model"], item["count"]))
        parts.append("%s ×%d (%s)" % (label, total, " / ".join(bits)))

    return {
        "count": count,
        "summary": " · ".join(parts),
        "inventory": inventory,
    }


def merge_inventories(inventories: Iterable[Iterable[Dict[str, Any]]]) -> Dict[str, Any]:
    """把多台主机的库存按 vendor+model 合并（集群盘点）。"""
    groups: "OrderedDict[Tuple[str, str], Dict[str, Any]]" = OrderedDict()
    for inv in inventories or []:
        for item in inv or []:
            if not isinstance(item, dict):
                continue
            vkey = _text(item.get("vendor")).lower() or "other"
            model = _text(item.get("model")) or "GPU"
            gkey = (vkey, model)
            if gkey not in groups:
                groups[gkey] = {
                    "vendor": vkey,
                    "vendor_label": _text(item.get("vendor_label"))
                    or vendor_summary_label(vkey),
                    "model": model,
                    "name": _text(item.get("name")) or model,
                    "count": 0,
                }
            try:
                groups[gkey]["count"] += int(item.get("count") or 0)
            except (TypeError, ValueError):
                pass
    inventory = list(groups.values())
    vendor_order: "OrderedDict[str, List[Dict[str, Any]]]" = OrderedDict()
    for item in inventory:
        vendor_order.setdefault(item["vendor"], []).append(item)
    parts: List[str] = []
    total = 0
    for vkey, items in vendor_order.items():
        label = items[0]["vendor_label"]
        vtotal = sum(int(x["count"]) for x in items)
        total += vtotal
        if len(items) == 1:
            model = items[0]["model"]
            parts.append(
                "%s ×%d %s" % (label, vtotal, model) if model else "%s ×%d" % (label, vtotal)
            )
        else:
            bits = ["%s ×%d" % (x["model"], x["count"]) for x in items]
            parts.append("%s ×%d (%s)" % (label, vtotal, " / ".join(bits)))
    return {
        "count": total,
        "summary": " · ".join(parts),
        "inventory": inventory,
    }


def accel_utils(cards: Iterable[Dict[str, Any]]) -> List[float]:
    vals: List[float] = []
    for card in cards or []:
        if not isinstance(card, dict):
            continue
        try:
            if card.get("util_percent") is None:
                continue
            vals.append(float(card["util_percent"]))
        except (TypeError, ValueError):
            continue
    return vals
