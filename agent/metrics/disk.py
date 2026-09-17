"""多挂载点磁盘采集：本地真实文件系统，过滤 tmpfs/overlay 等噪音。

汇总规则（写入兼容字段）：
- disk_total_gb / disk_used_gb：纳入列表的挂载点容量求和
- disk_percent：各挂载点利用率的最大值（告警看最满的盘，而不是被大盘摊薄）
- disks[]：分盘明细

Python 3.6 兼容：无 walrus、无 list[str]、无 future annotations。
"""

import os
import subprocess
from typing import Any, Dict, Iterable, List, Optional

# 1 GiB：过小的 EFI/引导分区不进列表（配置的 disk_path 始终保留）
MIN_TOTAL_BYTES = 1024 * 1024 * 1024

_SKIP_FSTYPES = frozenset(
    [
        "autofs",
        "binfmt_misc",
        "bpf",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devfs",
        "devpts",
        "devtmpfs",
        "efivarfs",
        "fdescfs",
        "fuse",
        "fusectl",
        "hugetlbfs",
        "iso9660",
        "mqueue",
        "nfs",
        "nfs4",
        "nfsd",
        "none",
        "nsfs",
        "overlay",
        "overlay2",
        "pipefs",
        "proc",
        "pstore",
        "ramfs",
        "rpc_pipefs",
        "securityfs",
        "selinuxfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
        "udev",
        "9p",
        "afs",
        "ceph",
        "cifs",
        "smb",
        "smb3",
        "smbfs",
    ]
)

_FUSE_KEEP = frozenset(
    ["fuseblk", "fuse.ntfs-3g", "fuse.ntfs", "fuse.exfat", "fuse.fuseblk"]
)

_SKIP_MOUNT_PREFIXES = (
    "/proc",
    "/sys",
    "/dev",
    "/snap",
    "/var/lib/docker",
    "/var/lib/containerd",
    "/var/lib/containers",
    "/var/lib/kubelet",
    "/var/lib/rancher",
    "/boot/efi",
    "/run/user",
    "/run/containerd",
    "/run/docker",
    "/run/snapd",
    "/System/Volumes/Preboot",
    "/System/Volumes/Recovery",
    "/System/Volumes/VM",
    "/System/Volumes/Update",
    "/System/Volumes/Hardware",
    "/System/Volumes/iSCPreboot",
    "/System/Volumes/xarts",
)

_SKIP_MOUNT_EXACT = frozenset(["/proc", "/sys", "/dev", "/run", "/snap"])

_SKIP_DF_DEVICES = frozenset(
    ["devfs", "map", "tmpfs", "none", "overlay", "udev", "proc", "sysfs"]
)


def unescape_proc_field(value: str) -> str:
    """还原 /proc/mounts 中的八进制转义（空格 \\040 等）。"""
    text = value or ""
    out = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\" and i + 3 < n:
            octal = text[i + 1 : i + 4]
            if len(octal) == 3 and all(ch in "01234567" for ch in octal):
                out.append(chr(int(octal, 8)))
                i += 4
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


def norm_mount(path: str) -> str:
    p = (path or "/").strip() or "/"
    if len(p) > 1:
        p = p.rstrip("/")
    return p or "/"


def _path_prefixed(path: str, prefix: str) -> bool:
    p = norm_mount(path)
    pre = norm_mount(prefix)
    if p == pre:
        return True
    return p.startswith(pre + "/")


def fstype_is_skipped(fstype: str) -> bool:
    fs = (fstype or "").strip().lower()
    if not fs:
        return False
    if fs in _SKIP_FSTYPES:
        return True
    if fs.startswith("nfs"):
        return True
    if fs.startswith("fuse."):
        return fs not in _FUSE_KEEP
    return False


def mount_is_noise(device: str, mount: str, fstype: str) -> bool:
    """虚拟/网络/容器叠层等噪音挂载。"""
    mnt = norm_mount(mount)
    if mnt in _SKIP_MOUNT_EXACT:
        return True
    for prefix in _SKIP_MOUNT_PREFIXES:
        if _path_prefixed(mnt, prefix):
            return True
    if fstype_is_skipped(fstype):
        return True
    dev = (device or "").strip()
    dev_l = dev.lower()
    if dev_l in _SKIP_DF_DEVICES:
        return True
    if dev_l.startswith("map "):
        return True
    return False


def parse_proc_mounts(text: str) -> List[Dict[str, str]]:
    """解析 /proc/mounts 或 /etc/mtab。"""
    rows: List[Dict[str, str]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        rows.append(
            {
                "device": unescape_proc_field(parts[0]),
                "mount": unescape_proc_field(parts[1]),
                "fstype": unescape_proc_field(parts[2]),
                "options": unescape_proc_field(parts[3]) if len(parts) > 3 else "",
            }
        )
    return rows


def parse_df_kp(text: str) -> List[Dict[str, Any]]:
    """解析 POSIX `df -kP`（macOS / 无 /proc/mounts 时回退）。"""
    rows: List[Dict[str, Any]] = []
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return rows
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        device = parts[0]
        mount = parts[-1]
        try:
            total_k = int(float(parts[1]))
            used_k = int(float(parts[2]))
        except (TypeError, ValueError):
            continue
        rows.append(
            {
                "device": device,
                "mount": mount,
                "fstype": "",
                "total_bytes": max(0, total_k * 1024),
                "used_bytes": max(0, used_k * 1024),
            }
        )
    return rows


def usage_from_bytes(total_bytes: int, used_bytes: int) -> Dict[str, float]:
    total = max(0, int(total_bytes or 0))
    used = max(0, int(used_bytes or 0))
    if used > total > 0:
        used = total
    percent = round(used * 100.0 / total, 2) if total else 0.0
    return {
        "total_gb": round(total / 1024.0 / 1024.0 / 1024.0, 2),
        "used_gb": round(used / 1024.0 / 1024.0 / 1024.0, 2),
        "percent": percent,
    }


def statvfs_usage(path: str) -> Optional[Dict[str, Any]]:
    try:
        st = os.statvfs(path)
        total = int(st.f_frsize) * int(st.f_blocks)
        free = int(st.f_frsize) * int(st.f_bavail)
        used = total - free
        if used < 0:
            used = 0
        try:
            dev_id = os.stat(path).st_dev
        except OSError:
            dev_id = None
        out = usage_from_bytes(total, used)
        out["total_bytes"] = total
        out["used_bytes"] = used
        out["dev_id"] = dev_id
        return out
    except OSError:
        return None


def select_local_mounts(
    candidates: Iterable[Dict[str, Any]],
    primary_path: str = "/",
    min_total_bytes: int = MIN_TOTAL_BYTES,
) -> List[Dict[str, Any]]:
    """过滤噪音/过小盘，并按块设备去重（bind / 同池子卷只留一条）。"""
    primary = norm_mount(primary_path)
    min_bytes = int(min_total_bytes) if min_total_bytes is not None else MIN_TOTAL_BYTES
    picked: List[Dict[str, Any]] = []
    for raw in candidates:
        if not isinstance(raw, dict):
            continue
        device = str(raw.get("device") or "")
        mount = norm_mount(str(raw.get("mount") or ""))
        fstype = str(raw.get("fstype") or "")
        is_primary = mount == primary
        if not is_primary and mount_is_noise(device, mount, fstype):
            continue
        try:
            total_b = int(raw.get("total_bytes") or 0)
        except (TypeError, ValueError):
            total_b = 0
        if total_b <= 0:
            continue
        if not is_primary and total_b < min_bytes:
            continue
        try:
            used_b = int(raw.get("used_bytes") or 0)
        except (TypeError, ValueError):
            used_b = 0
        usage = usage_from_bytes(total_b, used_b)
        item = {
            "mount": mount,
            "device": device,
            "fstype": fstype,
            "total_gb": usage["total_gb"],
            "used_gb": usage["used_gb"],
            "percent": usage["percent"],
            "total_bytes": total_b,
            "used_bytes": used_b,
            "dev_id": raw.get("dev_id"),
            "_primary": is_primary,
        }
        picked.append(item)

    # 同设备去重：优先保留配置的主路径，否则最短挂载路径
    grouped: Dict[Any, Dict[str, Any]] = {}
    anonymous = 0
    for item in picked:
        key = item.get("dev_id")
        if key is None:
            dev = item.get("device") or ""
            key = ("dev", dev) if dev else ("anon", anonymous)
            if not dev:
                anonymous += 1
        prev = grouped.get(key)
        if prev is None:
            grouped[key] = item
            continue
        if item.get("_primary") and not prev.get("_primary"):
            grouped[key] = item
            continue
        if prev.get("_primary") and not item.get("_primary"):
            continue
        if len(item["mount"]) < len(prev["mount"]):
            grouped[key] = item

    selected = list(grouped.values())
    selected.sort(
        key=lambda d: (-float(d.get("percent") or 0), str(d.get("mount") or ""))
    )
    return selected


def summarize_disks(disks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """生成兼容字段 + disks 列表（去掉内部字段）。"""
    public: List[Dict[str, Any]] = []
    total_b = 0
    used_b = 0
    max_pct = 0.0
    for d in disks or []:
        total_b += int(d.get("total_bytes") or 0)
        used_b += int(d.get("used_bytes") or 0)
        try:
            pct = float(d.get("percent") or 0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct > max_pct:
            max_pct = pct
        public.append(
            {
                "mount": d.get("mount") or "/",
                "device": d.get("device") or "",
                "fstype": d.get("fstype") or "",
                "total_gb": d.get("total_gb"),
                "used_gb": d.get("used_gb"),
                "percent": d.get("percent"),
            }
        )
    if not public:
        return {
            "disk_total_gb": 0,
            "disk_used_gb": 0,
            "disk_percent": 0.0,
            "disk_count": 0,
            "disks": [],
        }
    summed = usage_from_bytes(total_b, used_b)
    return {
        "disk_total_gb": summed["total_gb"],
        "disk_used_gb": summed["used_gb"],
        "disk_percent": round(max_pct, 2),
        "disk_count": len(public),
        "disks": public,
    }


def _candidates_from_proc_mounts() -> List[Dict[str, Any]]:
    text = None
    for path in ("/proc/mounts", "/etc/mtab"):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
            break
        except OSError:
            continue
    if not text:
        return []
    out: List[Dict[str, Any]] = []
    for row in parse_proc_mounts(text):
        usage = statvfs_usage(row["mount"])
        if not usage:
            continue
        item = dict(row)
        item.update(usage)
        out.append(item)
    return out


def _candidates_from_df() -> List[Dict[str, Any]]:
    try:
        text = subprocess.check_output(
            ["df", "-kP"],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    out: List[Dict[str, Any]] = []
    for row in parse_df_kp(text):
        usage = statvfs_usage(str(row.get("mount") or "/"))
        if usage:
            row["dev_id"] = usage.get("dev_id")
            # 以 statvfs 为准（与旧 sample_disk 口径一致）
            row["total_bytes"] = usage["total_bytes"]
            row["used_bytes"] = usage["used_bytes"]
        out.append(row)
    return out


def _ensure_primary(
    candidates: List[Dict[str, Any]], primary_path: str
) -> List[Dict[str, Any]]:
    primary = norm_mount(primary_path)
    for row in candidates:
        if norm_mount(str(row.get("mount") or "")) == primary:
            return candidates
    usage = statvfs_usage(primary_path)
    if not usage:
        usage = statvfs_usage(primary)
    if not usage:
        return candidates
    extra = {
        "device": "",
        "mount": primary,
        "fstype": "",
        "options": "",
    }
    extra.update(usage)
    return list(candidates) + [extra]


def collect_disks(disk_path: str = "/", min_total_bytes: int = MIN_TOTAL_BYTES) -> Dict[str, Any]:
    """采集多挂载点并给出汇总。失败时与旧版 sample_disk 一样返回 0。"""
    primary = disk_path or "/"
    candidates = _candidates_from_proc_mounts()
    if not candidates:
        candidates = _candidates_from_df()
    candidates = _ensure_primary(candidates, primary)
    selected = select_local_mounts(
        candidates, primary_path=primary, min_total_bytes=min_total_bytes
    )
    if not selected:
        usage = statvfs_usage(primary)
        if not usage:
            return {
                "disk_total_gb": 0,
                "disk_used_gb": 0,
                "disk_percent": 0.0,
                "disk_count": 0,
                "disks": [],
            }
        selected = [
            {
                "mount": norm_mount(primary),
                "device": "",
                "fstype": "",
                "total_gb": usage["total_gb"],
                "used_gb": usage["used_gb"],
                "percent": usage["percent"],
                "total_bytes": usage["total_bytes"],
                "used_bytes": usage["used_bytes"],
                "dev_id": usage.get("dev_id"),
            }
        ]
    return summarize_disks(selected)


def sample_disk(path: str = "/") -> Dict[str, float]:
    """单路径容量（兼容旧调用）。"""
    usage = statvfs_usage(path)
    if not usage:
        return {"disk_total_gb": 0, "disk_used_gb": 0, "disk_percent": 0.0}
    return {
        "disk_total_gb": usage["total_gb"],
        "disk_used_gb": usage["used_gb"],
        "disk_percent": usage["percent"],
    }
