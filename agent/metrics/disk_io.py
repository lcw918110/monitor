"""整机磁盘读写速率与设备忙闲（类似 iostat %util）。

容量仍由 disk.py 的 disk_percent / disks[] 表示，这里不读挂载占用。
Linux 读 /proc/diskstats，用上一次快照做差。进程刚启动还没有上一笔时，
速率和忙闲为 None，不编造 0。

扇区按内核约定的 512 字节。Mbps 与网络字段同一口径：字节 × 8 / 10^6，
不是 iostat 的 MB/s。没有额定磁盘带宽，忙闲用设备繁忙时间百分比，并限制在 0～100。

只计整盘（sda、nvme0n1、vda、mmcblk0），不计分区，以免把 sda 和 sda1 加两次。
loop / ram / zram / sr 以及 dm、md 等不计入。某一行解析失败时丢掉该行，其余保留。

Python 3.6 兼容：无 walrus、无 list[str]、无 future annotations。
"""

import re
import time
from typing import Any, Dict, Optional

# /proc/diskstats 的 sector 在内核文档里固定为 512 字节，与物理扇区无关。
SECTOR_BYTES = 512

_WHOLE_DISK_RE = re.compile(
    r"^(?:(?:sd|vd|xvd|hd)[a-z]+|nvme\d+n\d+|mmcblk\d+)$"
)

# 虚拟盘、光驱、设备映射和软 RAID。即使名字碰巧像整盘也不计入。
_NOISE_PREFIXES = (
    "loop",
    "ram",
    "zram",
    "sr",
    "fd",
    "dm-",
    "md",
    "nbd",
    "zd",
    "rbd",
    "drbd",
)

_STATE = {"mono": None, "devices": {}}


def reset_disk_io_state() -> None:
    """测试或进程内重采时清掉上一笔。"""
    _STATE["mono"] = None
    _STATE["devices"] = {}


def is_whole_data_disk(name: str) -> bool:
    """整块数据盘。分区（sda1、nvme0n1p1）和虚拟设备返回 False。"""
    text = (name or "").strip().lower()
    if not text:
        return False
    for prefix in _NOISE_PREFIXES:
        if text.startswith(prefix):
            return False
    return _WHOLE_DISK_RE.match(text) is not None


def _parse_int(token: Optional[str]) -> Optional[int]:
    if token is None:
        return None
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def parse_diskstats_line(line: str) -> Optional[Dict[str, Any]]:
    """解析一行。字段不够或名字为空则返回 None，不抛错。

    内核 2.6 起至少 14 列（含 io time）；4.18 起在后面追加 discard / flush。
    读扇区是第 6 列，写扇区是第 10 列，做 I/O 的毫秒是第 13 列（下标 5 / 9 / 12）。
    缺后面的列时，已有的计数仍保留。
    """
    text = (line or "").strip()
    if not text or text.startswith("#"):
        return None
    parts = text.split()
    if len(parts) < 3:
        return None
    name = parts[2]
    if not name or name.isdigit():
        return None
    return {
        "name": name,
        "read_sectors": _parse_int(parts[5] if len(parts) > 5 else None),
        "write_sectors": _parse_int(parts[9] if len(parts) > 9 else None),
        "io_ms": _parse_int(parts[12] if len(parts) > 12 else None),
    }


def parse_proc_diskstats(text: str) -> Dict[str, Dict[str, Any]]:
    """设备名 → 计数。坏行跳过。同名时保留后出现的一行。"""
    devices = {}
    for line in (text or "").splitlines():
        row = parse_diskstats_line(line)
        if not row:
            continue
        devices[row["name"]] = row
    return devices


def _counter_delta(prev: Any, curr: Any) -> Optional[int]:
    """计数回绕或无法解析时返回 None，避免把重启当成巨大速率。"""
    try:
        old = int(prev)
        new = int(curr)
    except (TypeError, ValueError):
        return None
    delta = new - old
    if delta < 0:
        return None
    return delta


def _mbps_from_sectors(delta_sectors: Optional[int], dt: float) -> Optional[float]:
    if delta_sectors is None or dt <= 0:
        return None
    mbps = (float(delta_sectors) * SECTOR_BYTES * 8.0) / 1000000.0 / dt
    if mbps < 0:
        return None
    return round(mbps, 3)


def _util_percent(delta_ms: Optional[int], dt: float) -> Optional[float]:
    """设备忙闲百分比。超过 100 的多队列计数压到 100，不另造额定带宽。"""
    if delta_ms is None or dt <= 0:
        return None
    pct = (float(delta_ms) / (dt * 1000.0)) * 100.0
    if pct < 0:
        return None
    if pct > 100.0:
        pct = 100.0
    return round(pct, 2)


def _empty_io() -> Dict[str, Any]:
    return {
        "disk_read_mbps": None,
        "disk_write_mbps": None,
        "disk_io_util_percent": None,
        "disk_io": [],
    }


def rates_between(
    prev: Dict[str, Dict[str, Any]],
    curr: Dict[str, Dict[str, Any]],
    dt: float,
) -> Dict[str, Any]:
    """两笔整盘计数的差。

    读/写 Mbps 为纳入磁盘之和；忙闲为其中最大的一块（与 disk_percent 取最满挂载同一精神）。
    只在两笔里都出现、且至少有一项能算出的磁盘进入 disk_io。
    没有任何可算项时，主机字段为 None 而不是 0。
    """
    try:
        span = float(dt)
    except (TypeError, ValueError):
        return _empty_io()
    if span <= 0:
        return _empty_io()
    names = sorted(set(prev or {}) & set(curr or {}))
    items = []
    reads = []
    writes = []
    utils = []
    for name in names:
        if not is_whole_data_disk(name):
            continue
        before = prev.get(name) or {}
        after = curr.get(name) or {}
        read = _mbps_from_sectors(
            _counter_delta(before.get("read_sectors"), after.get("read_sectors")),
            span,
        )
        write = _mbps_from_sectors(
            _counter_delta(before.get("write_sectors"), after.get("write_sectors")),
            span,
        )
        util = _util_percent(
            _counter_delta(before.get("io_ms"), after.get("io_ms")),
            span,
        )
        if read is None and write is None and util is None:
            continue
        items.append(
            {
                "name": name,
                "read_mbps": read,
                "write_mbps": write,
                "util_percent": util,
            }
        )
        if read is not None:
            reads.append(read)
        if write is not None:
            writes.append(write)
        if util is not None:
            utils.append(util)
    return {
        "disk_read_mbps": round(sum(reads), 3) if reads else None,
        "disk_write_mbps": round(sum(writes), 3) if writes else None,
        "disk_io_util_percent": round(max(utils), 2) if utils else None,
        "disk_io": items,
    }


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError:
        return None


def collect_disk_io(
    path: str = "/proc/diskstats",
    now: Optional[float] = None,
    state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """读 diskstats 并与 state 里的上一笔做差。默认用模块内状态。

    文件不存在时不覆盖上一笔，返回空字段。第一笔成功读取只记账，速率为 None。
    """
    box = state if state is not None else _STATE
    text = _read_text(path)
    if text is None:
        return _empty_io()
    parsed = parse_proc_diskstats(text)
    whole = {}
    for name, row in parsed.items():
        if is_whole_data_disk(name):
            whole[name] = row
    if now is None:
        stamp = time.monotonic()
    else:
        try:
            stamp = float(now)
        except (TypeError, ValueError):
            return _empty_io()
    prev_stamp = box.get("mono")
    prev_devices = box.get("devices") or {}
    box["mono"] = stamp
    box["devices"] = whole
    if prev_stamp is None:
        return _empty_io()
    try:
        dt = stamp - float(prev_stamp)
    except (TypeError, ValueError):
        return _empty_io()
    return rates_between(prev_devices, whole, dt)
