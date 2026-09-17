"""系统基础指标采集（以 /proc 与 df 为主）。

CPU 采集：
- 指标口径主要跟 **操作系统** 有关（Linux `/proc`、macOS 回退），
  x86_64 / aarch64（ARM）在 Linux 上走同一套 `/proc` 路径即可。
- 仍上报 `cpu_arch` / `os_name` / `cpu_model`，便于区分处理器架构与机型。
"""

import os
import platform
import socket
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple


def _read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def _cpu_times() -> Optional[Tuple[int, int]]:
    text = _read_text("/proc/stat")
    if not text:
        return None
    for line in text.splitlines():
        if line.startswith("cpu "):
            parts = line.split()
            # user nice system idle iowait irq softirq steal ...
            nums = [int(x) for x in parts[1:]]
            idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
            total = sum(nums)
            return total, idle
    return None


def _cpu_percent_macos(interval: float = 0.2) -> float:
    """用 top 采样 macOS CPU（开发机回退，生产以 Linux /proc 为主）。"""
    delay = max(1, int(round(interval))) if interval >= 0.5 else 1
    try:
        out = subprocess.check_output(
            ["top", "-l", "2", "-n", "0", "-s", str(delay)],
            universal_newlines=True,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0

    idle = None
    for line in out.splitlines():
        if "CPU usage" not in line:
            continue
        # 例: CPU usage: 8.45% user, 7.93% sys, 83.61% idle
        parts = line.replace(",", "").split()
        for i, tok in enumerate(parts):
            if tok == "idle" and i > 0 and parts[i - 1].endswith("%"):
                try:
                    idle = float(parts[i - 1].rstrip("%"))
                except ValueError:
                    idle = None
    if idle is None:
        return 0.0
    return round(max(0.0, min(100.0, 100.0 - idle)), 2)


def sample_cpu_percent(interval: float = 0.2) -> float:
    a = _cpu_times()
    if not a:
        return _cpu_percent_macos(interval)
    time.sleep(interval)
    b = _cpu_times()
    if not b:
        return 0.0
    total_d = b[0] - a[0]
    idle_d = b[1] - a[1]
    if total_d <= 0:
        return 0.0
    used = 1.0 - (idle_d / float(total_d))
    return round(max(0.0, min(100.0, used * 100.0)), 2)


def _memory_macos() -> Dict[str, float]:
    try:
        total = int(
            subprocess.check_output(
                ["sysctl", "-n", "hw.memsize"], universal_newlines=True
            ).strip()
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"mem_total_mb": 0, "mem_used_mb": 0, "mem_percent": 0.0}

    page_size = 4096
    try:
        page_size = int(
            subprocess.check_output(["pagesize"], universal_newlines=True).strip()
            or "4096"
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass

    stats: Dict[str, int] = {}
    try:
        out = subprocess.check_output(["vm_stat"], universal_newlines=True)
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            digits = "".join(ch for ch in v if ch.isdigit())
            if digits:
                stats[k.strip()] = int(digits)
    except (OSError, subprocess.SubprocessError):
        stats = {}

    # pages：wired + active + speculative + compressor ≈ 已用（近似）
    used_pages = (
        stats.get("Pages wired down", 0)
        + stats.get("Pages active", 0)
        + stats.get("Pages speculative", 0)
        + stats.get("Pages occupied by compressor", 0)
    )
    used = used_pages * page_size
    used = min(used, total)
    return {
        "mem_total_mb": round(total / 1024 / 1024, 1),
        "mem_used_mb": round(used / 1024 / 1024, 1),
        "mem_percent": round(used * 100.0 / total, 2) if total else 0.0,
    }


def sample_memory() -> Dict[str, float]:
    text = _read_text("/proc/meminfo")
    if not text:
        return _memory_macos()

    kv: Dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        num = v.strip().split()[0]
        try:
            kv[k] = int(num)  # kB
        except ValueError:
            continue

    total = kv.get("MemTotal", 0)
    available = kv.get("MemAvailable")
    if available is None:
        available = kv.get("MemFree", 0) + kv.get("Buffers", 0) + kv.get("Cached", 0)
    used = max(0, total - available)
    return {
        "mem_total_mb": round(total / 1024.0, 1),
        "mem_used_mb": round(used / 1024.0, 1),
        "mem_percent": round(used * 100.0 / total, 2) if total else 0.0,
    }


def sample_disk(path: str = "/") -> Dict[str, float]:
    from agent.metrics.disk import sample_disk as _sample_disk

    return _sample_disk(path)


def sample_load() -> Dict[str, float]:
    try:
        a, b, c = os.getloadavg()
        return {
            "load1": round(a, 2),
            "load5": round(b, 2),
            "load15": round(c, 2),
        }
    except (OSError, AttributeError):
        text = _read_text("/proc/loadavg")
        if not text:
            return {"load1": 0.0, "load5": 0.0, "load15": 0.0}
        parts = text.split()
        try:
            return {
                "load1": round(float(parts[0]), 2),
                "load5": round(float(parts[1]), 2) if len(parts) > 1 else 0.0,
                "load15": round(float(parts[2]), 2) if len(parts) > 2 else 0.0,
            }
        except (ValueError, IndexError):
            return {"load1": 0.0, "load5": 0.0, "load15": 0.0}


def sample_load1() -> float:
    return sample_load()["load1"]


def sample_uptime() -> int:
    text = _read_text("/proc/uptime")
    if text:
        try:
            return int(float(text.split()[0]))
        except (ValueError, IndexError):
            pass
    # macOS 回退
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "kern.boottime"], universal_newlines=True
        )
        # { sec = 123, usec = 0 } ...
        if "sec =" in out:
            sec = int(out.split("sec =")[1].split(",")[0].strip())
            return max(0, int(time.time()) - sec)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return 0


def sample_cpu_freq() -> Dict[str, Optional[float]]:
    """实时当前频率与额定最高频率（sysfs cpufreq，kHz→MHz）。不可用则字段为 None。"""
    n = min(os.cpu_count() or 1, 256)
    curs: List[float] = []
    maxes: List[float] = []
    for i in range(n):
        cur = _read_text("/sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % i)
        if not cur:
            cur = _read_text("/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_cur_freq" % i)
        mx = _read_text("/sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq" % i)
        cv = None
        mv = None
        try:
            if cur:
                cv = float(cur.strip().split()[0])
        except (TypeError, ValueError):
            cv = None
        try:
            if mx:
                mv = float(mx.strip().split()[0])
        except (TypeError, ValueError):
            mv = None
        if cv and cv > 0:
            curs.append(cv)
        if mv and mv > 0:
            maxes.append(mv)
    return {
        "cpu_freq_mhz": round(sum(curs) / len(curs) / 1000.0, 1) if curs else None,
        "cpu_freq_max_mhz": round(max(maxes) / 1000.0, 1) if maxes else None,
    }


def sample_cpu_identity() -> Dict[str, str]:
    arch = platform.machine() or ""
    os_name = platform.system() or ""
    model = ""
    text = _read_text("/proc/cpuinfo")
    if text:
        for line in text.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            key = k.strip().lower()
            if key in ("model name", "hardware", "cpu part", "cpu model"):
                model = v.strip()
                if key == "model name":
                    break
    if not model:
        try:
            model = subprocess.check_output(
                ["sysctl", "-n", "machdep.cpu.brand_string"],
                universal_newlines=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
        except (OSError, subprocess.SubprocessError):
            model = platform.processor() or ""
    return {
        "cpu_arch": arch,
        "os_name": os_name,
        "cpu_model": model,
    }


def sample_primary_ip() -> Optional[str]:
    """本机主要 IPv4（避开回环）。旧 Agent 不上报此字段时，中心仍可用部署清单/来源 IP。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(0.2)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
    except OSError:
        return None
    if not ip or ip.startswith("127.") or ip.startswith("0.") or ip.startswith("169.254."):
        return None
    return ip


def collect_system(disk_path: str = "/", cpu_sample_sec: float = 0.2) -> Dict[str, Any]:
    from agent.metrics.disk import collect_disks
    from agent.metrics.network import collect_network

    data: Dict[str, Any] = {
        "cpu_percent": sample_cpu_percent(cpu_sample_sec),
        "cpu_count": os.cpu_count() or 1,
        "uptime_sec": sample_uptime(),
    }
    data.update(sample_cpu_identity())
    data.update(sample_cpu_freq())
    data.update(sample_load())
    data.update(sample_memory())
    data.update(collect_disks(disk_path))
    data.update(collect_network())
    primary_ip = sample_primary_ip()
    if primary_ip:
        data["primary_ip"] = primary_ip
    return data
