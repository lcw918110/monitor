"""重点数据机上的采集。

路径只数个数、大小和修改时间，可选地给元数据做有上限的指纹。不打开文件内容。
外传速率用本轮已经算好的 net_tx_mbps，不另造网卡计数。
外连用 ss / netstat 的连接表做汇总。可移动盘只看挂载和 sysfs 的 removable。
"""

import hashlib
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from common.data_guard import is_archive_name, sanitize


_PROC_RE = re.compile(r'\(\("([^"]+)"')
_MAX_FILES = 8000
_MAX_DEPTH = 6
_MAX_SECONDS = 2.0
_FINGERPRINT_MAX = 2000
_BESIDE_MAX = 2000
_ARCHIVE_NAME_CAP = 400
_TX_CAP = 500

_MEDIA_PREFIXES = ("/media/", "/run/media/", "/mnt/")
_REMOVABLE_FS = {"vfat", "exfat", "msdos", "ntfs", "fuseblk", "ntfs3"}


def unescape_mount(text):
    # type: (str) -> str
    return (
        str(text or "")
        .replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def parse_mount_table(text):
    # type: (str) -> List[Dict[str, str]]
    rows = []
    for line in str(text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        rows.append(
            {
                "device": unescape_mount(parts[0]),
                "mount": unescape_mount(parts[1]),
                "fstype": parts[2],
            }
        )
    return rows


def block_disk_name(device):
    # type: (str) -> str
    name = str(device or "")
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name.startswith("nvme") or name.startswith("mmcblk"):
        head, sep, tail = name.rpartition("p")
        if sep and tail.isdigit() and head:
            return head
        return name
    trimmed = name
    while trimmed and trimmed[-1].isdigit():
        trimmed = trimmed[:-1]
    return trimmed or name


def select_removable_mounts(mounts, removable_disks):
    # type: (Sequence[Dict[str, str]], Set[str]) -> List[Dict[str, str]]
    chosen = []
    seen = set()
    removable_disks = set(removable_disks or [])
    for item in mounts:
        mount = item.get("mount") or ""
        if mount in ("/", "/mnt", "/media", "/run/media"):
            continue
        fstype = str(item.get("fstype") or "").lower()
        if fstype in (
            "nfs",
            "nfs4",
            "cifs",
            "smb3",
            "tmpfs",
            "devtmpfs",
            "overlay",
            "proc",
            "sysfs",
            "squashfs",
        ):
            continue
        disk = block_disk_name(item.get("device") or "")
        on_media = mount.startswith(_MEDIA_PREFIXES)
        removable = disk in removable_disks or (on_media and fstype in _REMOVABLE_FS)
        if not removable:
            continue
        if mount in seen:
            continue
        seen.add(mount)
        chosen.append(
            {
                "mount": mount,
                "device": str(item.get("device") or ""),
                "fstype": fstype,
            }
        )
    return chosen


def read_removable_disks(sys_block="/sys/block"):
    # type: (str) -> Set[str]
    found = set()
    if not os.path.isdir(sys_block):
        return found
    for name in os.listdir(sys_block):
        path = os.path.join(sys_block, name, "removable")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                flag = handle.read().strip()
        except OSError:
            continue
        if flag == "1":
            found.add(name)
    return found


def split_host_port(token):
    # type: (str) -> Tuple[Optional[str], Optional[str]]
    text = str(token or "").strip()
    if text.startswith("["):
        host, sep, port = text[1:].partition("]:")
        if not sep:
            return None, None
        return host, port
    if text.count(":") == 1:
        host, port = text.rsplit(":", 1)
        return host, port
    return None, None


def parse_socket_table(text):
    # type: (str) -> List[Dict[str, Any]]
    """解析 ss 或 netstat 的已建立连接，按目的地址、端口、进程名汇总。"""
    counts = {}  # type: Dict[Tuple[str, int, str], int]
    for line in str(text or "").splitlines():
        raw = line.strip()
        if not raw:
            continue
        upper = raw.upper()
        if "ESTAB" not in upper:
            continue
        if upper.startswith("STATE") or upper.startswith("NETID") or upper.startswith("PROTO"):
            continue
        match = _PROC_RE.search(raw)
        process = match.group(1).split("/")[-1] if match else ""
        tokens = raw.split()
        remote = ""
        for token in tokens:
            if token.startswith("users:"):
                break
            host, port = split_host_port(token)
            if host and port and port.isdigit():
                remote = token
        if not remote:
            continue
        host, port_text = split_host_port(remote)
        if not host or not port_text or not port_text.isdigit():
            continue
        if _skip_address(host):
            continue
        key = (host, int(port_text), process)
        counts[key] = counts.get(key, 0) + 1
    rows = []
    for (host, port, process), count in sorted(counts.items()):
        rows.append({"ip": host, "port": port, "process": process, "count": count})
    return rows


def _skip_address(host):
    # type: (str) -> bool
    text = str(host or "").strip().lower()
    if text in ("*", "0.0.0.0", "::", "::1", "127.0.0.1", "localhost"):
        return True
    if text.startswith("127.") or text.startswith("fe80:") or text.startswith("169.254."):
        return True
    return False


def read_socket_table():
    # type: () -> Tuple[Optional[str], str]
    commands = (
        ["ss", "-H", "-t", "-n", "-p", "state", "established"],
        ["netstat", "-tn"],
    )
    for command in commands:
        try:
            out = subprocess.check_output(
                command,
                stderr=subprocess.DEVNULL,
                universal_newlines=True,
                timeout=2,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        return out, ""
    return None, "read_failed"


def allowlisted_processes(names, proc_root="/proc"):
    # type: (Sequence[str], str) -> List[str]
    """只返回白名单里、并且当前确实在运行的进程名。不收集其它进程。"""
    wanted = []
    for name in names or []:
        text = str(name or "").strip()
        if text and text not in wanted:
            wanted.append(text)
    if not wanted or not os.path.isdir(proc_root):
        return []
    wanted_set = set(wanted)
    found = []
    try:
        entries = os.listdir(proc_root)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        path = os.path.join(proc_root, entry, "comm")
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as handle:
                comm = handle.read().strip()
        except OSError:
            continue
        if comm in wanted_set and comm not in found:
            found.append(comm)
        if len(found) >= len(wanted_set):
            break
    return found


def _fingerprint(rows):
    # type: (List[Tuple[str, int, int]]) -> Optional[str]
    if len(rows) > _FINGERPRINT_MAX:
        return None
    lines = ["%s\t%s\t%s" % item for item in sorted(rows)]
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return digest[:32]


def inventory_tree(root, now=None, max_files=_MAX_FILES, max_seconds=_MAX_SECONDS):
    # type: (str, Optional[float], int, float) -> Dict[str, Any]
    """清点一个文件或目录。只使用 lstat，不打开文件。"""
    started = time.time()
    now = now if now is not None else started
    result = {
        "path": root,
        "file_count": None,
        "total_bytes": None,
        "newest_mtime": None,
        "complete": False,
        "error": "",
        "meta_fingerprint": None,
        "archives": [],
    }  # type: Dict[str, Any]
    try:
        st = os.lstat(root)
    except OSError:
        result["error"] = "read_failed"
        return result
    if not os.path.isdir(root) or os.path.islink(root):
        size = int(st.st_size)
        mtime = int(st.st_mtime)
        name = os.path.basename(root)
        archives = []
        if is_archive_name(name):
            archives.append({"name": name, "bytes": size, "where": "under"})
        result.update(
            {
                "file_count": 1,
                "total_bytes": size,
                "newest_mtime": mtime,
                "complete": True,
                "archives": archives,
                "meta_fingerprint": _fingerprint([(name, size, mtime)]),
            }
        )
        return result

    count = 0
    total = 0
    newest = 0
    meta = []  # type: List[Tuple[str, int, int]]
    archives = []
    complete = True
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        depth = 0 if rel_dir == "." else rel_dir.count(os.sep) + 1
        if depth > _MAX_DEPTH:
            dirnames[:] = []
            complete = False
            continue
        if time.time() - started > max_seconds:
            complete = False
            break
        dirnames.sort()
        for filename in filenames:
            if count >= max_files or time.time() - started > max_seconds:
                complete = False
                break
            full = os.path.join(dirpath, filename)
            try:
                info = os.lstat(full)
            except OSError:
                continue
            if os.path.isdir(full):
                continue
            size = int(info.st_size)
            mtime = int(info.st_mtime)
            count += 1
            total += size
            if mtime > newest:
                newest = mtime
            rel = filename if rel_dir == "." else os.path.join(rel_dir, filename)
            if len(meta) < _FINGERPRINT_MAX:
                meta.append((rel, size, mtime))
            if is_archive_name(filename) and len(archives) < _ARCHIVE_NAME_CAP:
                archives.append({"name": filename, "bytes": size, "where": "under"})
        if not complete:
            break
    if not complete and count == 0 and total == 0:
        result["error"] = "read_failed"
        return result
    result.update(
        {
            "file_count": count,
            "total_bytes": total,
            "newest_mtime": newest or None,
            "complete": complete,
            "archives": archives,
            "meta_fingerprint": _fingerprint(meta) if complete else None,
        }
    )
    return result


def archives_beside(root, min_bytes, limit=_BESIDE_MAX):
    # type: (str, int, int) -> List[Dict[str, Any]]
    parent = os.path.dirname(root.rstrip("/")) or "/"
    if parent == "/":
        return []
    own = os.path.basename(root.rstrip("/"))
    found = []
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    if len(names) > limit:
        return []
    for name in names:
        if name == own or name.startswith("."):
            continue
        full = os.path.join(parent, name)
        try:
            info = os.lstat(full)
        except OSError:
            continue
        if os.path.isdir(full) or os.path.islink(full):
            continue
        if not is_archive_name(name) or int(info.st_size) < min_bytes:
            continue
        found.append({"name": name, "bytes": int(info.st_size), "where": "beside"})
    return found


def diff_archives(current, previous_names):
    # type: (List[Dict[str, Any]], Set[str]) -> List[Dict[str, Any]]
    fresh = []
    for item in current:
        name = str(item.get("name") or "")
        if name and name not in previous_names:
            fresh.append(item)
    return fresh


def read_mounts(path="/proc/mounts"):
    # type: (str) -> Tuple[Optional[str], str]
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read(), ""
    except OSError:
        return None, "read_failed"


def collect_observation(policy, state, net_tx_mbps, now, mounts_text=None, socket_text=None, proc_root="/proc"):
    # type: (Dict[str, Any], Dict[str, Any], Optional[float], int, Optional[str], Optional[str], str) -> Dict[str, Any]
    """根据缓存的策略采一笔。state 会被更新（速率窗口、路径基线）。"""
    tx = list(state.get("tx") or [])
    if net_tx_mbps is not None:
        try:
            mbps = float(net_tx_mbps)
        except (TypeError, ValueError):
            mbps = None
        if mbps is not None and mbps == mbps and mbps >= 0:
            tx.append({"ts": int(now), "mbps": round(mbps, 3)})
    horizon = max(
        int(policy.get("egress_volume_window_seconds") or 3600),
        int(policy.get("egress_sustain_seconds") or 60),
        3600,
    )
    tx = [item for item in tx if int(now) - int(item.get("ts") or 0) <= horizon + 60]
    if len(tx) > _TX_CAP:
        tx = tx[-_TX_CAP:]
    state["tx"] = tx

    paths_state = dict(state.get("paths") or {})
    interval = 300
    last_inventory = int(state.get("last_inventory_ts") or 0)
    missing_path = False
    for path in policy.get("paths") or []:
        if path not in paths_state:
            missing_path = True
            break
    do_inventory = missing_path or (int(now) - last_inventory) >= interval or not paths_state
    path_rows = []
    if do_inventory:
        for path in policy.get("paths") or []:
            current = inventory_tree(path, now=now)
            beside = []
            if not current.get("error"):
                beside = archives_beside(
                    path, int(policy.get("archive_min_bytes") or 100 * 1024 * 1024)
                )
            previous = paths_state.get(path) if isinstance(paths_state.get(path), dict) else None
            prev_names = set()
            if isinstance(previous, dict):
                for item in previous.get("archives") or []:
                    if isinstance(item, dict) and item.get("name"):
                        prev_names.add(str(item.get("name")))
                    elif isinstance(item, str):
                        prev_names.add(item)
            current_archives = list(current.get("archives") or []) + beside
            fresh = diff_archives(current_archives, prev_names) if previous else []
            # 第一笔不报新归档，只记住名字。
            row = {
                "path": path,
                "file_count": current.get("file_count"),
                "total_bytes": current.get("total_bytes"),
                "newest_mtime": current.get("newest_mtime"),
                "complete": current.get("complete"),
                "error": current.get("error") or "",
                "meta_fingerprint": current.get("meta_fingerprint"),
                "new_archives": fresh,
                "previous": None,
            }
            if isinstance(previous, dict) and not previous.get("error"):
                row["previous"] = {
                    "file_count": previous.get("file_count"),
                    "total_bytes": previous.get("total_bytes"),
                    "complete": previous.get("complete"),
                    "error": previous.get("error") or "",
                    "archives": [str(item.get("name") if isinstance(item, dict) else item) for item in (previous.get("archives") or [])],
                }
            path_rows.append(row)
            if not current.get("error"):
                remembered = []
                for item in current_archives:
                    name = str(item.get("name") or "")
                    if name and name not in remembered:
                        remembered.append(name)
                paths_state[path] = {
                    "file_count": current.get("file_count"),
                    "total_bytes": current.get("total_bytes"),
                    "newest_mtime": current.get("newest_mtime"),
                    "complete": current.get("complete"),
                    "error": "",
                    "archives": remembered[:_ARCHIVE_NAME_CAP],
                }
        state["paths"] = paths_state
        state["last_inventory_ts"] = int(now)
    else:
        for path in policy.get("paths") or []:
            previous = paths_state.get(path) if isinstance(paths_state.get(path), dict) else None
            if not isinstance(previous, dict):
                continue
            path_rows.append(
                {
                    "path": path,
                    "file_count": previous.get("file_count"),
                    "total_bytes": previous.get("total_bytes"),
                    "newest_mtime": previous.get("newest_mtime"),
                    "complete": previous.get("complete"),
                    "error": previous.get("error") or "",
                    "meta_fingerprint": None,
                    "new_archives": [],
                    "previous": {
                        "file_count": previous.get("file_count"),
                        "total_bytes": previous.get("total_bytes"),
                        "complete": previous.get("complete"),
                        "archives": list(previous.get("archives") or []),
                    },
                }
            )

    if mounts_text is None:
        mounts_text, mounts_error = read_mounts()
    else:
        mounts_error = ""
    if mounts_text is None:
        mounts = None  # type: Optional[List[Dict[str, str]]]
    else:
        mounts = select_removable_mounts(parse_mount_table(mounts_text), read_removable_disks())
        mounts_error = ""

    if socket_text is None:
        socket_text, socket_error = read_socket_table()
    else:
        socket_error = ""
    if socket_text is None:
        connections = None  # type: Optional[List[Dict[str, Any]]]
    else:
        connections = parse_socket_table(socket_text)
        socket_error = ""

    observation = {
        "paths": path_rows,
        "mounts": mounts,
        "mounts_error": mounts_error,
        "connections": connections,
        "connections_error": socket_error,
        "tx_samples": tx,
        "net_tx_mbps": net_tx_mbps,
        "allowlisted_processes": allowlisted_processes(
            policy.get("allow_processes") or [], proc_root=proc_root
        ),
    }
    return sanitize(observation)
