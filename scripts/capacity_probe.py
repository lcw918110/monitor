#!/usr/bin/env python3
"""单进程 SQLite 中心的实验室容量探针。

往一个临时库写入合成上报，走中心现有的上报入口（center.api.handle_metrics_post，
即 POST /api/v1/metrics 使用的函数；--http 时再经本机 127.0.0.1）。
然后按本次测到的每行字节，外推 retention_days 下的历史上报行数和库体积。

这不是 SLA，也不给出「能撑多少台」。数字只描述这一次运行。
冒烟模式只证明命令能跑完，外推不能写进结果表。
不生成诊断报告，不连接现场 /opt/monitor，不做双中心。
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.api import (  # noqa: E402
    handle_cluster_period_stats,
    handle_host_history,
    handle_host_period_stats,
    handle_hosts_list,
    handle_metrics_post,
)
from center.storage import Storage  # noqa: E402

HOST_PREFIX = "capprobe-"
DEFAULT_MAX_WRITES = 20000
SMOKE = {
    "hosts": 2,
    "interval_seconds": 15,
    "retention_days": 7,
    "samples_per_host": 2,
}


class CapacityError(Exception):
    """探针拒绝继续时的说明。"""


def samples_per_host_per_day(interval_seconds: int) -> int:
    if isinstance(interval_seconds, bool) or not isinstance(interval_seconds, int):
        raise ValueError("采样间隔必须是正整数秒")
    if interval_seconds <= 0:
        raise ValueError("采样间隔必须是正整数秒")
    return (86400 + interval_seconds - 1) // interval_seconds


def projected_history_rows(hosts: int, interval_seconds: int, retention_days: int) -> int:
    for name, value in (
        ("主机数", hosts),
        ("保留天数", retention_days),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("%s必须是正整数" % name)
    return hosts * samples_per_host_per_day(interval_seconds) * retention_days


def projected_bytes(history_rows: int, bytes_per_row: float) -> int:
    if isinstance(history_rows, bool) or not isinstance(history_rows, int) or history_rows < 0:
        raise ValueError("行数必须是非负整数")
    if bytes_per_row < 0 or not math.isfinite(bytes_per_row):
        raise ValueError("每行字节必须是有限的非负数")
    return int(round(history_rows * float(bytes_per_row)))


def percentile_ms(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (float(pct) / 100.0) * (len(ordered) - 1)
    lo = int(math.floor(rank))
    hi = int(math.ceil(rank))
    if lo == hi:
        return round(ordered[lo], 3)
    weight = rank - lo
    return round(ordered[lo] * (1.0 - weight) + ordered[hi] * weight, 3)


def laboratory_warnings(mode: str) -> List[str]:
    lines = [
        "实验室估算，不是 SLA。",
        "没有主机上限。不要把外推行数或库体积写成能撑多少台。",
        "双中心、双活、SQLite 多写和自动故障切换都没有做，本探针也不测切换。",
        "外推 = 主机数 × ceil(86400/采样间隔) × retention_days，再乘本次测得的每行字节。",
        "每行字节是整个库文件除以历史上报行数，含索引和主机表，不是单一 payload 长度。",
        "时段统计单次读取有 6 万行上限。月报、40 天保留、15 秒采样会碰到这道上限。",
        "合成主机的 host_id 以 capprobe- 开头，只存在于这次实验室库，不会生成诊断报告。",
    ]
    if mode == "smoke":
        lines.insert(0, "本次是冒烟：只证明命令能跑完。外推数字不能写进结果表，也不能当成容量结论。")
    else:
        lines.append("可以把本次 measured 和 projection 抄进结果表，并写上机器与时间。仍不要改写成主机上限。")
    return lines


def assert_lab_dir(path: str) -> str:
    """拒绝现场安装目录。容量探针只写实验室库。"""
    if not path:
        raise CapacityError("缺少实验室目录")
    raw = os.path.abspath(os.path.expanduser(path))
    real = os.path.realpath(raw)
    homes = [
        os.path.abspath(os.path.expanduser("~/monitor")),
        os.path.abspath(os.path.expanduser("~/monitor-agent")),
        os.path.realpath(os.path.expanduser("~/monitor")),
        os.path.realpath(os.path.expanduser("~/monitor-agent")),
    ]
    blocked = ["/opt/monitor", "/opt/monitor-agent"] + homes
    for candidate in (raw, real):
        norm = candidate.rstrip(os.sep) or os.sep
        for prefix in blocked:
            base = prefix.rstrip(os.sep) or os.sep
            if norm == base or norm.startswith(base + os.sep):
                raise CapacityError(
                    "拒绝在现场安装目录做容量探针: %s 。请换一个临时目录。" % path
                )
    if os.path.isfile(os.path.join(real, "config", "center.json")):
        raise CapacityError("目录里已有 config/center.json，这像现场安装，容量探针拒绝写入: %s" % real)
    return real


def _lab_payload(index: int, ts: int, with_accel: bool) -> Dict[str, Any]:
    host_id = "%s%04d" % (HOST_PREFIX, index)
    payload: Dict[str, Any] = {
        "host_id": host_id,
        "hostname": host_id,
        "host_type": "cpu",
        "timestamp": int(ts),
        "system": {
            "cpu_percent": 12.5,
            "cpu_count": 8,
            "mem_percent": 40.0,
            "mem_total_mb": 32000,
            "mem_used_mb": 12800,
            "disk_percent": 33.0,
            "disk_total_gb": 1000,
            "disk_used_gb": 330,
            "load1": 0.4,
            "load5": 0.3,
            "load15": 0.2,
            "net_rx_mbps": 1.2,
            "net_tx_mbps": 0.4,
            "disks": [
                {"mount": "/", "percent": 33.0, "used_gb": 330, "total_gb": 1000},
            ],
        },
        "gpus": [],
        "npus": [],
        "accelerators": [],
    }
    if with_accel:
        payload["gpus"] = [
            {
                "index": 0,
                "name": "capprobe-fake",
                "util_percent": 10,
                "mem_percent": 5,
                "mem_used_mb": 256,
                "mem_total_mb": 8192,
                "temp_c": 40,
                "power_w": 30,
            }
        ]
        payload["host_type"] = "gpu"
    return payload


def _footprint(db_path: str) -> int:
    total = 0
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.isfile(path):
            total += os.path.getsize(path)
    return total


def _report_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        try:
            return int(conn.execute("SELECT COUNT(*) FROM diagnostic_reports").fetchone()[0])
        except sqlite3.Error:
            return 0
    finally:
        conn.close()


def _history_rows(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM metrics_history").fetchone()[0])
    finally:
        conn.close()


class _ProbeServer:
    def __init__(self, storage: Storage) -> None:
        from http.server import ThreadingHTTPServer

        from center.server import make_handler

        handler = make_handler(storage, "")

        class Quiet(handler):
            def log_message(self, fmt: str, *args: Any) -> None:
                return

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="capprobe-http", daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.httpd.server_address[:2]
        return "http://%s:%s" % (host, port)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _http_json(method: str, url: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        status = exc.code
    payload = json.loads(raw.decode("utf-8"))
    if status != 200:
        raise CapacityError("HTTP %s %s -> %s %s" % (method, url, status, payload))
    return payload


def _timed(fn: Any) -> tuple:
    start = time.perf_counter()
    value = fn()
    return (time.perf_counter() - start) * 1000.0, value


def run_probe(
    hosts: int,
    interval_seconds: int,
    retention_days: int,
    samples_per_host: int,
    work_dir: str,
    http: bool = False,
    with_accel: bool = False,
    max_writes: int = DEFAULT_MAX_WRITES,
    mode: str = "sample",
) -> Dict[str, Any]:
    """写入实验室库并返回测量和外推。不写诊断报告，不给出主机上限。"""
    if isinstance(samples_per_host, bool) or not isinstance(samples_per_host, int) or samples_per_host < 1:
        raise CapacityError("每台样本数必须是正整数")
    if isinstance(max_writes, bool) or not isinstance(max_writes, int) or max_writes < 1:
        raise CapacityError("--max-writes 必须是正整数")
    try:
        projected_rows = projected_history_rows(hosts, interval_seconds, retention_days)
    except ValueError as exc:
        raise CapacityError(str(exc)) from exc
    writes = hosts * samples_per_host
    if writes > max_writes:
        raise CapacityError(
            "本次要写 %d 条，超过 --max-writes %d。这是单次实验室写入上限，不是容量结论。确认磁盘后再提高 --max-writes。"
            % (writes, max_writes)
        )
    work = assert_lab_dir(work_dir)
    os.makedirs(work, exist_ok=True)
    db_path = os.path.join(work, "data", "monitor.db")
    if os.path.exists(db_path):
        raise CapacityError("目标库已存在，拒绝接着写: %s 。换一个目录。" % db_path)
    storage = Storage(db_path, offline_seconds=90, retention_days=retention_days)
    server: Optional[_ProbeServer] = _ProbeServer(storage) if http else None
    ingest_ms: List[float] = []
    first_ts = 0
    last_ts = 0
    try:
        now = int(time.time())
        first_ts = now - (samples_per_host - 1) * interval_seconds
        for index in range(1, hosts + 1):
            for sample in range(samples_per_host):
                ts = first_ts + sample * interval_seconds
                last_ts = ts
                payload = _lab_payload(index, ts, with_accel)
                if not str(payload["host_id"]).startswith(HOST_PREFIX):
                    raise CapacityError("探针主机 id 必须以 %s 开头" % HOST_PREFIX)

                def _write(payload: Dict[str, Any] = payload) -> None:
                    if server is not None:
                        _http_json("POST", server.base + "/api/v1/metrics", payload)
                        return
                    code, resp = handle_metrics_post(storage, json.dumps(payload).encode("utf-8"))
                    if code != 200 or not resp.get("ok"):
                        raise CapacityError("上报失败: %s %s" % (code, resp))

                elapsed, _ = _timed(_write)
                ingest_ms.append(elapsed)
        query = "from_ts=%d&to_ts=%d" % (first_ts - 1, last_ts + 1)
        host_id = "%s%04d" % (HOST_PREFIX, 1)

        def _read_hosts() -> Dict[str, Any]:
            if server is not None:
                return _http_json("GET", server.base + "/api/v1/hosts")
            _code, payload = handle_hosts_list(storage)
            return payload

        def _read_history() -> Dict[str, Any]:
            if server is not None:
                return _http_json("GET", server.base + "/api/v1/hosts/%s/history?%s" % (host_id, query))
            _code, payload = handle_host_history(storage, host_id, query)
            return payload

        def _read_host_period() -> Dict[str, Any]:
            if server is not None:
                return _http_json(
                    "GET", server.base + "/api/v1/hosts/%s/period-stats?%s" % (host_id, query)
                )
            _code, payload = handle_host_period_stats(storage, host_id, query)
            return payload

        def _read_cluster() -> Dict[str, Any]:
            if server is not None:
                return _http_json("GET", server.base + "/api/v1/period-stats?%s" % query)
            _code, payload = handle_cluster_period_stats(storage, query)
            return payload

        hosts_ms, hosts_body = _timed(_read_hosts)
        history_ms, history_body = _timed(_read_history)
        host_period_ms, host_period_body = _timed(_read_host_period)
        cluster_ms, cluster_body = _timed(_read_cluster)
    finally:
        if server is not None:
            server.close()

    seen = [h.get("host_id") for h in hosts_body.get("hosts") or []]
    if any(not str(item).startswith(HOST_PREFIX) for item in seen):
        raise CapacityError("实验室库里出现了非探针主机，已中止")
    rows = _history_rows(db_path)
    size = _footprint(db_path)
    per_row = (float(size) / float(rows)) if rows else 0.0
    reports = _report_count(db_path)
    truncated = bool(host_period_body.get("history_truncated") or cluster_body.get("history_truncated"))
    result = {
        "ok": True,
        "mode": mode,
        "laboratory_estimate": True,
        "sla": False,
        "capacity_claim": False,
        "published_host_limit": None,
        "dual_center": "deferred",
        "fill_results_table": mode != "smoke",
        "transport": "http" if http else "api",
        "knobs": {
            "hosts": hosts,
            "interval_seconds": interval_seconds,
            "retention_days": retention_days,
            "samples_per_host": samples_per_host,
            "with_accel": with_accel,
            "max_writes": max_writes,
        },
        "measured": {
            "history_rows": rows,
            "db_bytes": size,
            "bytes_per_history_row": round(per_row, 2),
            "diagnostic_reports": reports,
            "hosts_seen": len(seen),
            "history_points": history_body.get("count"),
            "history_truncated": truncated,
            "ingest_ms": {
                "count": len(ingest_ms),
                "p50": percentile_ms(ingest_ms, 50),
                "p95": percentile_ms(ingest_ms, 95),
                "mean": round(sum(ingest_ms) / len(ingest_ms), 3) if ingest_ms else None,
            },
            "reads_ms": {
                "hosts": round(hosts_ms, 3),
                "history": round(history_ms, 3),
                "host_period": round(host_period_ms, 3),
                "cluster_period": round(cluster_ms, 3),
            },
        },
        "projection": {
            "samples_per_host_per_day": samples_per_host_per_day(interval_seconds),
            "history_rows_at_retention": projected_rows,
            "db_bytes_at_retention": projected_bytes(projected_rows, per_row),
            "note": "按本次每行字节外推保留期满时的库体积。不是主机上限，不是 SLA。",
        },
        "warnings": laboratory_warnings(mode),
        "work_dir": work,
    }
    return result


def format_report(result: Dict[str, Any]) -> str:
    knobs = result["knobs"]
    measured = result["measured"]
    projection = result["projection"]
    lines = [
        "实验室容量探针。这不是 SLA，也没有主机上限。",
        "模式: %s    写入路径: %s" % (result["mode"], result["transport"]),
        "旋钮: 主机=%s  采样间隔=%ss  retention_days=%s  每台实际写入=%s"
        % (
            knobs["hosts"],
            knobs["interval_seconds"],
            knobs["retention_days"],
            knobs["samples_per_host"],
        ),
        "测得: 历史上报=%s 行  库文件=%s 字节  约 %s 字节/行  诊断报告=%s"
        % (
            measured["history_rows"],
            measured["db_bytes"],
            measured["bytes_per_history_row"],
            measured["diagnostic_reports"],
        ),
        "写入毫秒: p50=%s  p95=%s  平均=%s"
        % (
            measured["ingest_ms"]["p50"],
            measured["ingest_ms"]["p95"],
            measured["ingest_ms"]["mean"],
        ),
        "读取毫秒: 主机列表=%s  单机历史=%s  单机时段=%s  集群时段=%s  时段截断=%s"
        % (
            measured["reads_ms"]["hosts"],
            measured["reads_ms"]["history"],
            measured["reads_ms"]["host_period"],
            measured["reads_ms"]["cluster_period"],
            "是" if measured["history_truncated"] else "否",
        ),
        "外推（不是上限）: 每天每台 %s 点 × %s 天 = %s 行，约 %s 字节"
        % (
            projection["samples_per_host_per_day"],
            knobs["retention_days"],
            projection["history_rows_at_retention"],
            projection["db_bytes_at_retention"],
        ),
        "双中心 / 双活: 推迟，本探针不测切换。",
    ]
    if result["mode"] == "smoke":
        lines.append("冒烟结果不要填进 docs/15-capacity-lab.md 的结果表。")
    else:
        lines.append("若这是一次正式实验室运行，把上面的测得和外推填进结果表，并注明机器与时间。")
    lines.append("对比中心磁盘剩余空间时，升级还要额外留出大约 3 倍数据库。")
    lines.append("写入 p95 如果接近采样间隔，先加大间隔或减少主机，不要加第二个写库进程。")
    return "\n".join(lines)


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("必须是正整数")
    if value < 1:
        raise argparse.ArgumentTypeError("必须是正整数")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="估算单进程 SQLite 中心在给定主机数、采样间隔和保留天数下的实验室体量。不是 SLA。"
    )
    parser.add_argument("--smoke", action="store_true", help="固定 2 台 × 2 点，只验证能跑通，不产生容量结论")
    parser.add_argument("--hosts", type=_positive_int, help="模拟主机数。不传且没有 --smoke 时按冒烟运行")
    parser.add_argument("--interval", type=_positive_int, default=15, help="采样间隔秒，默认 15")
    parser.add_argument("--retention-days", type=_positive_int, default=7, help="外推用的保留天数，默认 7")
    parser.add_argument(
        "--samples-per-host",
        type=_positive_int,
        help="每台实际写入条数。默认正式跑 20，冒烟为 2。不是把保留期写满",
    )
    parser.add_argument("--http", action="store_true", help="经 127.0.0.1 调用 /api/v1/metrics 等现有接口")
    parser.add_argument("--with-accel", action="store_true", help="报文里带一块假加速卡，只为加大一点 payload")
    parser.add_argument("--max-writes", type=_positive_int, default=DEFAULT_MAX_WRITES)
    parser.add_argument("--keep-dir", help="把实验室库留在这个目录。不能是 /opt/monitor 或已有 center.json 的目录")
    parser.add_argument("--json-out", help="把机器可读结果写到这个文件。stdout 仍是给人看的说明")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.smoke and args.hosts is not None:
        print("--smoke 不能和 --hosts 一起用", file=sys.stderr)
        return 2
    if args.smoke or args.hosts is None:
        mode = "smoke"
        hosts = SMOKE["hosts"]
        interval = SMOKE["interval_seconds"]
        retention = SMOKE["retention_days"]
        samples = SMOKE["samples_per_host"]
        if args.hosts is None and not args.smoke:
            print("未指定 --hosts，按冒烟运行。正式估算请显式传入 --hosts。", file=sys.stderr)
    else:
        mode = "sample"
        hosts = args.hosts
        interval = args.interval
        retention = args.retention_days
        samples = args.samples_per_host or 20
    keep = args.keep_dir
    created = False
    if keep:
        try:
            assert_lab_dir(keep)
        except CapacityError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        os.makedirs(keep, exist_ok=True)
        work = keep
    else:
        work = tempfile.mkdtemp(prefix="capprobe-")
        created = True
    try:
        result = run_probe(
            hosts=hosts,
            interval_seconds=interval,
            retention_days=retention,
            samples_per_host=samples,
            work_dir=work,
            http=args.http,
            with_accel=args.with_accel,
            max_writes=args.max_writes,
            mode=mode,
        )
    except CapacityError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if created:
            shutil.rmtree(work, ignore_errors=True)
    if created:
        result["work_dir"] = None
    text = format_report(result)
    print(text)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print("JSON: %s" % args.json_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
