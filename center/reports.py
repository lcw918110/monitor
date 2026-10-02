"""中心端日/周/月使用诊断报告。

本地用时段统计（样本加权）聚合原始历史上报后入库。
读取已保存报告不会重算。不拼接旧的日/周报来拼月报。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs

from center.anomaly import judge_host_payload
from center.storage import PERIOD_MAX_ROWS, Storage


# Asia/Shanghai 无夏令时，固定 UTC+8，兼容 Center 的 Python 3.8。
SHANGHAI = timezone(timedelta(hours=8))

PERIOD_LABELS = {"day": "日报", "week": "周报", "month": "月报"}

# 基本不用已从旧模板的 CPU<15% / 加速卡<10% 收紧到 5%。
IDLE_AVG_LT = 5.0
IDLE_BUSY_LT = 0.05
HIGH_BUSY_GTE = 0.40
HIGH_P95_GTE = 85.0
REPORT_MIN_SAMPLES = 10
# 月报在 retention_days=40、15s 采样时会超过时段接口默认 6 万行上限。
REPORT_MAX_ROWS = 250000

RANKING_CSV_FIELDS = [
    "host_id",
    "address",
    "hostname",
    "group_name",
    "online",
    "pole",
    "pole_reason",
    "cpu_avg",
    "cpu_p95",
    "cpu_busy_ratio",
    "cpu_samples",
    "accel_count",
    "accel_summary",
    "accel_avg",
    "accel_p95",
    "accel_busy_ratio",
    "accel_samples",
    "mem_avg",
    "mem_p95",
    "disk_avg",
    "disk_p95",
    "disk_max",
    "disk_samples",
    "fullest_mount",
    "fullest_mount_percent",
]


def _fmt_ts(ts: Optional[int]) -> str:
    if ts is None:
        return "-"
    return datetime.fromtimestamp(int(ts), SHANGHAI).strftime("%Y-%m-%d %H:%M")


def _range_text(from_ts: int, to_ts: int) -> str:
    return "%s～%s" % (_fmt_ts(from_ts), _fmt_ts(to_ts))


def _floor_midnight(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def _add_months(month_start: datetime, delta: int) -> datetime:
    year = month_start.year
    month = month_start.month + delta
    while month <= 0:
        month += 12
        year -= 1
    while month > 12:
        month -= 12
        year += 1
    return month_start.replace(year=year, month=month, day=1)


def report_period_bounds(period: str, now: Optional[int] = None) -> Dict[str, Any]:
    """Asia/Shanghai 半开窗口。to_ts 为结束时刻，不含该秒。

    日报：昨日 00:00～今日 00:00，对比再前一日。
    周报：上周一 00:00～本周一 00:00，对比再前一周。
    月报：上月 1 日 00:00～本月 1 日 00:00，对比再上一自然月。
    """
    if period not in PERIOD_LABELS:
        raise ValueError("period 必须是 day、week 或 month")
    now_ts = int(now if now is not None else datetime.now(SHANGHAI).timestamp())
    local = datetime.fromtimestamp(now_ts, SHANGHAI)
    today = _floor_midnight(local)
    if period == "day":
        end = today
        start = today - timedelta(days=1)
        compare_end = start
        compare_start = start - timedelta(days=1)
    elif period == "week":
        this_monday = today - timedelta(days=today.weekday())
        end = this_monday
        start = this_monday - timedelta(days=7)
        compare_end = start
        compare_start = start - timedelta(days=7)
    else:
        end = _add_months(_floor_midnight(today).replace(day=1), 0)
        start = _add_months(end, -1)
        compare_end = start
        compare_start = _add_months(start, -1)
    from_ts = int(start.timestamp())
    to_ts = int(end.timestamp())
    compare_from = int(compare_start.timestamp())
    compare_to = int(compare_end.timestamp())
    return {
        "period": period,
        "label": PERIOD_LABELS[period],
        "from_ts": from_ts,
        "to_ts": to_ts,
        "compare_from_ts": compare_from,
        "compare_to_ts": compare_to,
        "range_text": _range_text(from_ts, to_ts),
        "compare_range_text": _range_text(compare_from, compare_to),
        "timezone": "Asia/Shanghai",
    }


def _metric(host: Dict[str, Any], key: str) -> Dict[str, Any]:
    return ((host.get("metrics") or {}).get(key) or {})


def _sample_count(metric: Dict[str, Any]) -> int:
    try:
        return int(metric.get("sample_count") or 0)
    except (TypeError, ValueError):
        return 0


def _enough(metric: Dict[str, Any], min_samples: int) -> bool:
    return _sample_count(metric) >= int(min_samples)


def host_has_accel(host: Dict[str, Any]) -> bool:
    try:
        if int(host.get("accel_count") or 0) > 0:
            return True
    except (TypeError, ValueError):
        pass
    return _sample_count(_metric(host, "accel_util_avg")) > 0


def high_load_reasons(
    host: Dict[str, Any], min_samples: int = REPORT_MIN_SAMPLES
) -> List[str]:
    """高负荷：CPU 或加速卡繁忙占比 ≥40%，或对应 P95 ≥85%。样本不足不判。"""
    reasons: List[str] = []
    cpu = _metric(host, "cpu_percent")
    if _enough(cpu, min_samples):
        busy = cpu.get("busy_ratio")
        p95 = cpu.get("p95")
        if busy is not None and float(busy) >= HIGH_BUSY_GTE:
            reasons.append("CPU 繁忙占比 %.1f%%" % (float(busy) * 100.0))
        if p95 is not None and float(p95) >= HIGH_P95_GTE:
            reasons.append("CPU P95 %.1f%%" % float(p95))
    if host_has_accel(host):
        accel = _metric(host, "accel_util_avg")
        if _enough(accel, min_samples):
            busy = accel.get("busy_ratio")
            p95 = accel.get("p95")
            if busy is not None and float(busy) >= HIGH_BUSY_GTE:
                reasons.append("加速卡繁忙占比 %.1f%%" % (float(busy) * 100.0))
            if p95 is not None and float(p95) >= HIGH_P95_GTE:
                reasons.append("加速卡 P95 %.1f%%" % float(p95))
    return reasons


def idle_status(
    host: Dict[str, Any], min_samples: int = REPORT_MIN_SAMPLES
) -> Tuple[bool, str]:
    """基本不用：当前在线、样本够，CPU 平均 <5% 且繁忙 <5%。

    有加速卡时，加速卡平均也须 <5% 且繁忙 <5%。达不到样本要求则不算空闲。
    """
    if not host.get("online"):
        return False, "当前离线，不计入基本不用"
    cpu = _metric(host, "cpu_percent")
    if not _enough(cpu, min_samples):
        return False, "CPU 样本不足"
    avg = cpu.get("avg")
    busy = cpu.get("busy_ratio")
    if avg is None or busy is None:
        return False, "CPU 样本不足"
    if not (float(avg) < IDLE_AVG_LT and float(busy) < IDLE_BUSY_LT):
        return False, ""
    if host_has_accel(host):
        accel = _metric(host, "accel_util_avg")
        if not _enough(accel, min_samples):
            return False, "加速卡样本不足"
        aavg = accel.get("avg")
        abusy = accel.get("busy_ratio")
        if aavg is None or abusy is None:
            return False, "加速卡样本不足"
        if not (float(aavg) < IDLE_AVG_LT and float(abusy) < IDLE_BUSY_LT):
            return False, ""
        return True, "CPU 与加速卡平均、繁忙占比均低于 5%"
    return True, "CPU 平均与繁忙占比均低于 5%；无加速卡"


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "/").replace("\n", " ").strip()


def _has_samples(metric: Dict[str, Any]) -> bool:
    return _sample_count(metric) > 0 and metric.get("avg") is not None


def _fmt_num(metric: Dict[str, Any], field: str, digits: int = 1, suffix: str = "") -> str:
    if not _has_samples(metric) or metric.get(field) is None:
        return "样本不足"
    return ("%." + str(digits) + "f") % float(metric[field]) + suffix


def _fmt_busy(metric: Dict[str, Any]) -> str:
    if not _sample_count(metric):
        return "样本不足"
    if metric.get("busy_ratio") is None:
        return "—"
    return "%.1f%%" % (float(metric["busy_ratio"]) * 100.0)


def _fmt_plain(value: Any, digits: int = 1, suffix: str = "") -> str:
    if value is None:
        return "样本不足"
    try:
        return ("%." + str(digits) + "f") % float(value) + suffix
    except (TypeError, ValueError):
        return "样本不足"


def _host_label(host: Dict[str, Any]) -> str:
    address = str(host.get("address") or "").strip()
    hid = str(host.get("host_id") or "").strip()
    if address and address != hid:
        return "%s（%s）" % (address, hid)
    return address or hid or "-"


def _section(title: str, paragraphs: Optional[List[str]] = None) -> Dict[str, Any]:
    return {"title": title, "paragraphs": list(paragraphs or []), "tables": [], "notes": []}


def _add_table(
    section: Dict[str, Any],
    headers: List[str],
    rows: List[List[Any]],
    caption: str = "",
) -> None:
    section["tables"].append(
        {
            "caption": caption,
            "headers": headers,
            "rows": [[_cell(c) for c in row] for row in rows],
        }
    )


def render_markdown(title: str, sections: List[Dict[str, Any]]) -> str:
    lines = ["# %s" % title, ""]
    for sec in sections:
        lines.append("## %s" % sec.get("title", ""))
        lines.append("")
        for para in sec.get("paragraphs") or []:
            lines.append(str(para))
            lines.append("")
        for table in sec.get("tables") or []:
            headers = table.get("headers") or []
            if not headers:
                continue
            if table.get("caption"):
                lines.append(str(table["caption"]))
                lines.append("")
            lines.append("| " + " | ".join(_cell(h) for h in headers) + " |")
            lines.append("| " + " | ".join("---" for _ in headers) + " |")
            for row in table.get("rows") or []:
                cells = list(row) + [""] * (len(headers) - len(row))
                lines.append("| " + " | ".join(_cell(c) for c in cells[: len(headers)]) + " |")
            lines.append("")
        for note in sec.get("notes") or []:
            lines.append(str(note))
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _query_window(
    storage: Storage, from_ts: int, to_ts: int, now: int
) -> Tuple[Optional[str], Dict[str, Any]]:
    """半开 [from, to) 转成时段接口的整数闭区间 [from, to-1]，再按保留期裁剪。"""
    if int(to_ts) <= int(from_ts):
        return "时间范围为空", {}
    return storage.resolve_period_window(
        from_ts=int(from_ts), to_ts=int(to_ts) - 1, now=int(now)
    )


def _cluster(
    storage: Storage,
    window: Dict[str, Any],
    busy: Dict[str, float],
    host_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return storage.cluster_period_stats(
        host_ids=host_ids,
        busy_thresholds=busy,
        window=window,
        max_rows=REPORT_MAX_ROWS,
    )


def _comment_cpu(metric: Dict[str, Any]) -> str:
    if not _has_samples(metric):
        return "样本不足"
    busy = metric.get("busy_ratio")
    p95 = metric.get("p95")
    avg = metric.get("avg")
    if (busy is not None and float(busy) >= HIGH_BUSY_GTE) or (
        p95 is not None and float(p95) >= HIGH_P95_GTE
    ):
        return "高负荷"
    if (
        avg is not None
        and busy is not None
        and float(avg) < IDLE_AVG_LT
        and float(busy) < IDLE_BUSY_LT
    ):
        return "整体偏低"
    return "介于两极之间"


def _comment_mem(metric: Dict[str, Any]) -> str:
    if not _has_samples(metric):
        return "样本不足；不参与闲忙"
    avg = metric.get("avg")
    p95 = metric.get("p95")
    if (avg is not None and float(avg) >= 85) or (p95 is not None and float(p95) >= 85):
        return "偏高；不参与闲忙"
    return "未到偏高线；不参与闲忙"


def _comment_disk(metric: Dict[str, Any], warn: float, critical: float) -> str:
    if not _has_samples(metric) or metric.get("max") is None:
        return "样本不足；不参与闲忙"
    maximum = float(metric["max"])
    if maximum >= critical:
        return "有样本达到容量临界；磁盘满不等于算力忙"
    if maximum >= warn:
        return "有样本容量偏高；磁盘满不等于算力忙"
    return "容量未到偏高线；不参与闲忙"


def _comment_accel(metric: Dict[str, Any]) -> str:
    if not _has_samples(metric):
        return "无加速卡或样本不足"
    return _comment_cpu(metric)


def _comment_load(metric: Dict[str, Any]) -> str:
    if not _has_samples(metric):
        return "样本不足；不参与闲忙"
    return "仅对照，不参与闲忙"


def _delta_sentence(current: Dict[str, Any], previous: Optional[Dict[str, Any]]) -> str:
    if not previous:
        return "上一周期无法对比（超出保留期或窗口无效），不编造涨跌。"
    prev_cluster = (previous.get("cluster") or {}).get("metrics") or {}
    cur_cluster = (current.get("cluster") or {}).get("metrics") or {}
    if not int(previous.get("sample_count") or 0):
        return "上一周期样本不足，无法对比涨跌。"
    bits: List[str] = []
    for key, label in (
        ("cpu_percent", "CPU"),
        ("accel_util_avg", "加速卡"),
        ("mem_percent", "内存"),
        ("disk_percent", "磁盘"),
    ):
        cur_m = cur_cluster.get(key) or {}
        prev_m = prev_cluster.get(key) or {}
        if not _has_samples(cur_m) or not _has_samples(prev_m):
            continue
        diff = float(cur_m["avg"]) - float(prev_m["avg"])
        if abs(diff) < 0.05:
            bits.append("%s平均与上一周期基本持平（%.1f%%）" % (label, float(cur_m["avg"])))
        else:
            word = "上升" if diff > 0 else "下降"
            bits.append(
                "%s平均较上一周期%s %.1f 个百分点（%.1f%% → %.1f%%）"
                % (label, word, abs(diff), float(prev_m["avg"]), float(cur_m["avg"]))
            )
    if not bits:
        return "上一周期或本周期关键指标样本不足，无法对比涨跌。"
    return "；".join(bits) + "。"


def _top_risk(
    offline_n: int,
    high_n: int,
    disk_critical_n: int,
    disk_warn_n: int,
) -> str:
    if disk_critical_n:
        return "优先关注磁盘达到临界的 %d 台主机；磁盘满不等于算力忙。" % disk_critical_n
    if high_n:
        return "优先关注高负荷主机 %d 台（繁忙占比或 P95）。" % high_n
    if disk_warn_n:
        return "磁盘有 %d 台达到容量偏高，建议分开看容量与算力。" % disk_warn_n
    if offline_n:
        return "当前有 %d 台离线，可用性优先于利用率。" % offline_n
    return "本窗口未发现磁盘临界或算力高负荷主机。"


def _sort_cpu(host: Dict[str, Any]) -> Tuple[int, float, float]:
    cpu = _metric(host, "cpu_percent")
    if not _sample_count(cpu):
        return (1, 0.0, 0.0)
    return (0, -float(cpu.get("busy_ratio") or 0), -float(cpu.get("p95") or 0))


def _sort_accel(host: Dict[str, Any]) -> Tuple[int, float, float]:
    accel = _metric(host, "accel_util_avg")
    if not _sample_count(accel):
        return (1, 0.0, 0.0)
    return (0, -float(accel.get("busy_ratio") or 0), -float(accel.get("p95") or 0))


def _sort_disk(host: Dict[str, Any]) -> Tuple[int, float]:
    disk = _metric(host, "disk_percent")
    if not _sample_count(disk) or disk.get("max") is None:
        return (1, 0.0)
    return (0, -float(disk["max"]))


def _metric_row(name: str, metric: Dict[str, Any], comment: str, as_percent: bool) -> List[str]:
    suffix = "%" if as_percent else ""
    digits = 1 if as_percent else 2
    return [
        name,
        _fmt_num(metric, "avg", digits, suffix),
        _fmt_num(metric, "p95", digits, suffix),
        _fmt_num(metric, "max", digits, suffix),
        _fmt_busy(metric),
        str(_sample_count(metric)) if _sample_count(metric) else "样本不足",
        comment,
    ]


def _rank_headers() -> List[str]:
    return ["地址", "资源组", "在线", "CPU 平均", "CPU P95", "CPU 繁忙", "加速卡平均", "加速卡 P95", "加速卡繁忙", "样本"]


def _rank_row(host: Dict[str, Any]) -> List[str]:
    cpu = _metric(host, "cpu_percent")
    accel = _metric(host, "accel_util_avg")
    return [
        _host_label(host),
        host.get("group_name") or "未分组",
        "在线" if host.get("online") else "离线",
        _fmt_num(cpu, "avg", 1, "%"),
        _fmt_num(cpu, "p95", 1, "%"),
        _fmt_busy(cpu),
        _fmt_num(accel, "avg", 1, "%") if host_has_accel(host) else "无加速卡",
        _fmt_num(accel, "p95", 1, "%") if host_has_accel(host) else "无加速卡",
        _fmt_busy(accel) if host_has_accel(host) else "无加速卡",
        str(host.get("sample_count") or 0),
    ]


def _mount_text(mount: Optional[Dict[str, Any]]) -> str:
    if not mount:
        return "挂载样本不足"
    name = mount.get("mount") or "（挂载名为空）"
    return "%s %.1f%%" % (name, float(mount.get("percent") or 0))


def build_report(
    storage: Storage,
    period: str,
    now: Optional[int] = None,
    busy_thresholds: Optional[Dict[str, float]] = None,
    anomaly_thresholds: Optional[Dict[str, Any]] = None,
    min_samples: int = REPORT_MIN_SAMPLES,
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """计算一份报告。成功返回 (None, doc)，失败返回 (error, None)。不写库。"""
    try:
        bounds = report_period_bounds(period, now=now)
    except ValueError as exc:
        return str(exc), None
    now_ts = int(now if now is not None else datetime.now(SHANGHAI).timestamp())
    err, window = _query_window(storage, bounds["from_ts"], bounds["to_ts"], now_ts)
    if err:
        return err, None
    busy = dict(busy_thresholds or {})
    if not busy:
        from center.storage import DEFAULT_BUSY_THRESHOLDS

        busy = dict(DEFAULT_BUSY_THRESHOLDS)
    anomaly = anomaly_thresholds or {}
    try:
        disk_warn = float(anomaly.get("disk_warn_percent") or 85)
    except (TypeError, ValueError):
        disk_warn = 85.0
    try:
        disk_critical = float(anomaly.get("disk_critical_percent") or 95)
    except (TypeError, ValueError):
        disk_critical = 95.0

    current = _cluster(storage, window, busy)
    compare_err, compare_window = _query_window(
        storage, bounds["compare_from_ts"], bounds["compare_to_ts"], now_ts
    )
    previous: Optional[Dict[str, Any]] = None
    compare_note = ""
    if compare_err:
        compare_note = "上一周期%s，不做对比。" % compare_err
    else:
        previous = _cluster(storage, compare_window, busy)
        if compare_window.get("clamped"):
            compare_note = "上一周期亦被保留期裁剪，对比只覆盖实际重叠到的样本。"

    hosts: List[Dict[str, Any]] = list(current.get("hosts") or [])
    cluster_metrics = (current.get("cluster") or {}).get("metrics") or {}
    total = len(hosts)
    online_n = sum(1 for h in hosts if h.get("online"))
    offline_hosts = [h for h in hosts if not h.get("online")]

    classified: List[Dict[str, Any]] = []
    for host in hosts:
        reasons = high_load_reasons(host, min_samples)
        high = bool(reasons)
        idle, idle_reason = idle_status(host, min_samples)
        if high:
            idle = False
            idle_reason = ""
        insufficient = ""
        if not high and not idle:
            if host.get("online") and idle_reason in ("CPU 样本不足", "加速卡样本不足"):
                insufficient = idle_reason
            elif host.get("online") and not _enough(_metric(host, "cpu_percent"), min_samples):
                insufficient = "CPU 样本不足"
        if high:
            pole = "high_load"
            pole_reason = "；".join(reasons)
        elif idle:
            pole = "idle"
            pole_reason = idle_reason
        elif insufficient:
            pole = "insufficient"
            pole_reason = insufficient
        elif not host.get("online"):
            pole = "offline"
            pole_reason = "当前离线"
        else:
            pole = "normal"
            pole_reason = ""
        classified.append(
            {
                "host": host,
                "pole": pole,
                "pole_reason": pole_reason,
                "high_reasons": reasons,
            }
        )

    high_rows = [c for c in classified if c["pole"] == "high_load"]
    idle_rows = [c for c in classified if c["pole"] == "idle"]
    insufficient_rows = [c for c in classified if c["pole"] == "insufficient"]

    disk_metric = cluster_metrics.get("disk_percent") or {}
    mounts: Dict[str, Optional[Dict[str, Any]]] = {}
    disk_interest = []
    for host in hosts:
        disk = _metric(host, "disk_percent")
        maximum = disk.get("max")
        if maximum is None or not _sample_count(disk):
            continue
        if float(maximum) >= disk_warn:
            disk_interest.append(host)
    disk_interest.sort(key=_sort_disk)
    for host in disk_interest:
        mounts[str(host.get("host_id"))] = storage.fullest_disk_mount(
            str(host.get("host_id")),
            int(window["from_ts"]),
            int(window["to_ts"]),
            max_rows=REPORT_MAX_ROWS,
        )
    disk_critical_hosts = [
        h
        for h in disk_interest
        if float(_metric(h, "disk_percent").get("max") or 0) >= disk_critical
    ]
    disk_warn_hosts = [
        h
        for h in disk_interest
        if disk_warn <= float(_metric(h, "disk_percent").get("max") or 0) < disk_critical
    ]

    cpu_top = sorted(hosts, key=_sort_cpu)[:8]
    accel_hosts = [h for h in hosts if host_has_accel(h)]
    accel_top = sorted(accel_hosts, key=_sort_accel)[:8]
    disk_top = sorted(hosts, key=_sort_disk)[:8]
    mem_high = [
        h
        for h in hosts
        if _enough(_metric(h, "mem_percent"), min_samples)
        and (
            float(_metric(h, "mem_percent").get("avg") or 0) >= 85
            or float(_metric(h, "mem_percent").get("p95") or 0) >= 85
        )
    ]
    mem_high.sort(
        key=lambda h: -float(_metric(h, "mem_percent").get("avg") or 0)
    )

    live_findings: List[Tuple[Dict[str, Any], str]] = []
    for host in hosts:
        if not host.get("online"):
            continue
        detail = storage.get_host(str(host.get("host_id")))
        if not detail:
            continue
        judged = judge_host_payload(
            detail.get("payload") or {},
            thresholds=anomaly,
            online=True,
        )
        overall = judged.get("overall")
        if overall not in ("warn", "critical"):
            continue
        messages = [
            f.get("message")
            for f in (judged.get("findings") or [])
            if f.get("message")
        ]
        live_findings.append((host, "；".join(messages) or overall))

    busy_cpu = busy.get("cpu_percent", 80)
    busy_accel = busy.get("accel_util_avg", 80)
    clamped = bool(window.get("clamped"))
    retention_days = window.get("retention_days")
    actual_from = int(window["from_ts"])
    actual_to_inclusive = int(window["to_ts"])
    title = "使用诊断%s（%s）" % (bounds["label"], bounds["range_text"])

    sections: List[Dict[str, Any]] = []

    head = _section("1. 抬头")
    head["paragraphs"] = [
        "类型：%s。时区 Asia/Shanghai，时间窗为半开区间（含起点、不含结束时刻）。"
        % bounds["label"],
        "时间窗：%s。" % bounds["range_text"],
        "对比窗：%s。" % bounds["compare_range_text"],
        "集群范围：中心已入库的全部主机（%d 台）。加速卡库存：%s。"
        % (
            total,
            current.get("accel_summary") or "无加速卡",
        ),
        "口径：集群 avg / P95 / 繁忙占比为样本加权（sample_weighted）。繁忙阈值 CPU≥%s%%、加速卡≥%s%%（可用 period_util 配置）。闲忙两极只看 CPU 与加速卡，磁盘和内存不进入闲忙。高负荷：繁忙占比≥40%% 或 P95≥85%%。基本不用：当前在线且该指标样本≥%d；CPU 平均 <5%% 且繁忙 <5%%；有加速卡则加速卡平均 <5%% 且繁忙 <5%%。缺数写样本不足，不编造。本报告直接聚合窗口内原始样本，不拼接已保存的日/周报。"
        % (busy_cpu, busy_accel, min_samples),
    ]
    if clamped:
        head["paragraphs"].append(
            "保留期裁剪：retention_days=%s。请求起点 %s 早于可查询范围，实际从 %s 纳入到 %s（含该秒）。报告抬头已注明 clamped。"
            % (
                retention_days,
                _fmt_ts(bounds["from_ts"]),
                _fmt_ts(actual_from),
                _fmt_ts(actual_to_inclusive),
            )
        )
    else:
        head["paragraphs"].append(
            "保留期：retention_days=%s。本窗口未被裁剪。" % retention_days
        )
    sections.append(head)

    overview = _section("2. 总览")
    rate = (100.0 * online_n / total) if total else None
    overview["paragraphs"] = [
        "在线 %d / 共 %d（%s）。离线 %d 台。在线率按生成时刻判定；离线主机不把末次快照当成实时用量。"
        % (
            online_n,
            total,
            ("%.1f%%" % rate) if rate is not None else "样本不足",
            len(offline_hosts),
        ),
        _delta_sentence(current, previous),
        "Top 风险：" + _top_risk(
            len(offline_hosts),
            len(high_rows),
            len(disk_critical_hosts),
            len(disk_warn_hosts),
        ),
    ]
    if compare_note:
        overview["notes"].append(compare_note)
    _add_table(
        overview,
        ["指标", "平均", "P95", "最高", "繁忙占比", "样本数"],
        [
            ["CPU%"] + _metric_row("", cluster_metrics.get("cpu_percent") or {}, "", True)[1:6],
            ["内存%"] + _metric_row("", cluster_metrics.get("mem_percent") or {}, "", True)[1:6],
            ["磁盘%"] + _metric_row("", cluster_metrics.get("disk_percent") or {}, "", True)[1:6],
            ["加速卡%"]
            + _metric_row("", cluster_metrics.get("accel_util_avg") or {}, "", True)[1:6],
            ["load1"]
            + _metric_row("", cluster_metrics.get("load1") or {}, "", False)[1:6],
        ],
    )
    sections.append(overview)

    util = _section("3. 集群资源利用表")
    util["paragraphs"] = [
        current.get("rollup_note")
        or "集群汇总为样本加权。",
        "简评里的高负荷 / 偏低只对 CPU 与加速卡使用闲忙阈值；磁盘、内存、load1 单独看。",
    ]
    _add_table(
        util,
        ["指标", "平均", "P95", "最高", "繁忙占比", "样本数", "简评"],
        [
            _metric_row("CPU%", cluster_metrics.get("cpu_percent") or {}, _comment_cpu(cluster_metrics.get("cpu_percent") or {}), True),
            _metric_row("内存%", cluster_metrics.get("mem_percent") or {}, _comment_mem(cluster_metrics.get("mem_percent") or {}), True),
            _metric_row(
                "磁盘%",
                disk_metric,
                _comment_disk(disk_metric, disk_warn, disk_critical),
                True,
            ),
            _metric_row(
                "加速卡%",
                cluster_metrics.get("accel_util_avg") or {},
                _comment_accel(cluster_metrics.get("accel_util_avg") or {}),
                True,
            ),
            _metric_row("load1", cluster_metrics.get("load1") or {}, _comment_load(cluster_metrics.get("load1") or {}), False),
        ],
    )
    sections.append(util)

    ranks = _section("4. 主机排行")
    ranks["paragraphs"] = [
        "地址用完整 IPv4（优先部署清单 / host_id 内嵌地址，跳过 198.18.0.0/15 隧道地址）。下列为窗口内历史聚合，不是离线主机的末次快照。",
    ]
    _add_table(
        ranks,
        _rank_headers(),
        [_rank_row(h) for h in cpu_top] or [["样本不足"] * len(_rank_headers())],
        caption="CPU 繁忙或 P95",
    )
    if accel_hosts:
        _add_table(
            ranks,
            _rank_headers(),
            [_rank_row(h) for h in accel_top],
            caption="加速卡繁忙或 P95",
        )
    else:
        ranks["notes"].append("加速卡排行：无加速卡或样本不足。")
    _add_table(
        ranks,
        ["磁盘占用（按窗口内最高）", "最高", "平均", "最满挂载"],
        [
            [
                _host_label(h),
                _fmt_num(_metric(h, "disk_percent"), "max", 1, "%"),
                _fmt_num(_metric(h, "disk_percent"), "avg", 1, "%"),
                _mount_text(mounts.get(str(h.get("host_id"))))
                if float(_metric(h, "disk_percent").get("max") or 0) >= disk_warn
                else "未单列挂载（未到容量偏高）",
            ]
            for h in disk_top
            if _sample_count(_metric(h, "disk_percent"))
        ]
        or [["样本不足", "样本不足", "样本不足", "样本不足"]],
    )
    if mem_high:
        _add_table(
            ranks,
            ["内存偏高", "平均", "P95", "资源组"],
            [
                [
                    _host_label(h),
                    _fmt_num(_metric(h, "mem_percent"), "avg", 1, "%"),
                    _fmt_num(_metric(h, "mem_percent"), "p95", 1, "%"),
                    h.get("group_name") or "未分组",
                ]
                for h in mem_high[:8]
            ],
        )
    else:
        ranks["notes"].append("内存偏高：无主机在样本足够时达到平均或 P95 ≥85%。内存不进入闲忙。")
    anomaly_rows: List[List[str]] = []
    for host in offline_hosts:
        anomaly_rows.append(
            [
                _host_label(host),
                host.get("group_name") or "未分组",
                "离线",
                "末次上报 %s；当前离线，不展示末次快照用量。窗口内样本 %s。"
                % (_fmt_ts(host.get("last_seen")), host.get("sample_count") or 0),
            ]
        )
    for host, message in live_findings:
        anomaly_rows.append(
            [
                _host_label(host),
                host.get("group_name") or "未分组",
                "当前快照判定",
                message + "（这是生成时刻的在线快照，不是窗口平均）",
            ]
        )
    if anomaly_rows:
        _add_table(ranks, ["异常 / 离线", "资源组", "类别", "说明"], anomaly_rows)
    else:
        ranks["notes"].append("异常 / 离线：无离线主机，当前在线主机也没有偏高或异常判定。")
    sections.append(ranks)

    poles = _section("5. 闲忙两极")
    poles["paragraphs"] = [
        "只看算力。磁盘、内存不在本节。高负荷优先：已列入高负荷的主机不再重复计入基本不用。",
        "基本不用条件：在线且样本≥%d；CPU 平均 <5%% 且繁忙 <5%%；有加速卡则加速卡平均 <5%% 且繁忙 <5%%。"
        % min_samples,
    ]
    if high_rows:
        _add_table(
            poles,
            ["高负荷", "资源组", "原因", "CPU 繁忙", "CPU P95", "加速卡繁忙", "加速卡 P95"],
            [
                [
                    _host_label(c["host"]),
                    c["host"].get("group_name") or "未分组",
                    c["pole_reason"],
                    _fmt_busy(_metric(c["host"], "cpu_percent")),
                    _fmt_num(_metric(c["host"], "cpu_percent"), "p95", 1, "%"),
                    _fmt_busy(_metric(c["host"], "accel_util_avg"))
                    if host_has_accel(c["host"])
                    else "无加速卡",
                    _fmt_num(_metric(c["host"], "accel_util_avg"), "p95", 1, "%")
                    if host_has_accel(c["host"])
                    else "无加速卡",
                ]
                for c in high_rows
            ],
        )
    else:
        poles["notes"].append("高负荷：无主机达到繁忙占比 ≥40% 或 P95 ≥85%（样本足够时）。")
    if idle_rows:
        _add_table(
            poles,
            ["基本不用", "资源组", "原因", "CPU 平均", "CPU 繁忙", "加速卡平均", "加速卡繁忙"],
            [
                [
                    _host_label(c["host"]),
                    c["host"].get("group_name") or "未分组",
                    c["pole_reason"],
                    _fmt_num(_metric(c["host"], "cpu_percent"), "avg", 1, "%"),
                    _fmt_busy(_metric(c["host"], "cpu_percent")),
                    _fmt_num(_metric(c["host"], "accel_util_avg"), "avg", 1, "%")
                    if host_has_accel(c["host"])
                    else "无加速卡",
                    _fmt_busy(_metric(c["host"], "accel_util_avg"))
                    if host_has_accel(c["host"])
                    else "无加速卡",
                ]
                for c in idle_rows
            ],
        )
    else:
        poles["notes"].append("基本不用：没有同时满足在线、样本足够且 CPU/加速卡都低于 5% 的主机。")
    if insufficient_rows:
        _add_table(
            poles,
            ["样本不足", "资源组", "原因"],
            [
                [
                    _host_label(c["host"]),
                    c["host"].get("group_name") or "未分组",
                    c["pole_reason"],
                ]
                for c in insufficient_rows
            ],
        )
    summary_bits = [
        "高负荷 %d 台，基本不用 %d 台。基本不用只统计当前在线、样本足够、且未落入高负荷的主机。"
        % (len(high_rows), len(idle_rows)),
    ]
    if high_rows and idle_rows:
        summary_bits.append(
            "两极同时存在，算力分布不均：空闲侧可评估合并或承接新任务，高负荷侧先区分短时尖峰（P95）和持续繁忙。"
        )
    elif idle_rows:
        summary_bits.append("存在低于 5% 的在线空闲算力，可评估合并或回收，不要把磁盘空当成算力空。")
    elif high_rows:
        summary_bits.append("有高负荷主机。繁忙占比高表示很多样本超过繁忙阈值；仅 P95 高则更像尖峰。")
    else:
        summary_bits.append("未形成明显的高负荷与基本不用两极。")
    summary_bits.append("磁盘与内存不参与上述划分。")
    poles["paragraphs"].extend(summary_bits[:3])
    sections.append(poles)

    disks = _section("6. 磁盘")
    disks["paragraphs"] = [
        "独立容量节，不进入闲忙。集群磁盘平均 %s，最高 %s，P95 %s。分档线 ≥%.0f%% / ≥%.0f%%（与 anomaly 磁盘阈值一致，默认 85/95）。"
        % (
            _fmt_num(disk_metric, "avg", 1, "%"),
            _fmt_num(disk_metric, "max", 1, "%"),
            _fmt_num(disk_metric, "p95", 1, "%"),
            disk_warn,
            disk_critical,
        ),
        "磁盘满不等于算力忙。多挂载只在历史点保存了 disks[] 时给出最满挂载；没有则写挂载样本不足，不用当前快照回填过去的窗口。",
    ]
    def _disk_table(title: str, rows_hosts: List[Dict[str, Any]]) -> None:
        if not rows_hosts:
            disks["notes"].append("%s：无。" % title)
            return
        _add_table(
            disks,
            [title, "窗口最高", "窗口平均", "最满挂载"],
            [
                [
                    _host_label(h),
                    _fmt_num(_metric(h, "disk_percent"), "max", 1, "%"),
                    _fmt_num(_metric(h, "disk_percent"), "avg", 1, "%"),
                    _mount_text(mounts.get(str(h.get("host_id")))),
                ]
                for h in rows_hosts
            ],
        )

    _disk_table("≥%.0f%%" % disk_critical, disk_critical_hosts)
    _disk_table("%.0f%%～%.0f%%" % (disk_warn, disk_critical), disk_warn_hosts)
    sections.append(disks)

    groups_sec = _section("7. 资源组")
    grouped: Dict[int, Dict[str, Any]] = {}
    ungrouped = 0
    for host in hosts:
        gid = host.get("group_id")
        if gid is None:
            ungrouped += 1
            continue
        bucket = grouped.setdefault(
            int(gid), {"name": host.get("group_name") or str(gid), "ids": []}
        )
        bucket["ids"].append(host["host_id"])
    if not grouped:
        groups_sec["paragraphs"] = ["当前没有资源组，本节无切片。未分组主机 %d 台。" % ungrouped]
    else:
        groups_sec["paragraphs"] = [
            "分组存在中心 SQLite，与 Agent 无关。组内 avg / 繁忙占比同样是样本加权。另有未分组主机 %d 台。"
            % ungrouped
        ]
        group_rows: List[List[str]] = []
        for gid in sorted(grouped, key=lambda i: grouped[i]["name"]):
            info = grouped[gid]
            stats = _cluster(storage, window, busy, host_ids=info["ids"])
            grow = stats.get("hosts") or []
            g_online = sum(1 for h in grow if h.get("online"))
            g_total = len(grow) or len(info["ids"])
            g_metrics = (stats.get("cluster") or {}).get("metrics") or {}
            busiest = sorted(grow, key=_sort_cpu)[:2]
            busiest_text = "、".join(
                _host_label(h) for h in busiest if _sample_count(_metric(h, "cpu_percent"))
            ) or "样本不足"
            online_rate = "%.0f%%" % (100.0 * g_online / g_total) if g_total else "样本不足"
            group_rows.append(
                [
                    info["name"],
                    str(g_total),
                    online_rate,
                    _fmt_num(g_metrics.get("cpu_percent") or {}, "avg", 1, "%"),
                    _fmt_busy(g_metrics.get("cpu_percent") or {}),
                    _fmt_num(g_metrics.get("accel_util_avg") or {}, "avg", 1, "%"),
                    _fmt_busy(g_metrics.get("accel_util_avg") or {}),
                    busiest_text,
                ]
            )
        _add_table(
            groups_sec,
            ["资源组", "主机数", "在线率", "CPU 平均", "CPU 繁忙", "加速卡平均", "加速卡繁忙", "组内最忙"],
            group_rows,
        )
    sections.append(groups_sec)

    advice = _section("8. 诊断与建议")
    advice_lines = [
        "容量：%s"
        % (
            "有主机磁盘达到临界或偏高，请按挂载扩容或清理；这不表示 CPU/加速卡忙。"
            if disk_interest
            else "窗口内磁盘最高未到偏高线，或磁盘样本不足。"
        ),
        "算力：高负荷 %d 台，基本不用 %d 台。空闲标准是平均与繁忙都低于 5%%，不再使用 15%%/10%%。"
        % (len(high_rows), len(idle_rows)),
        "可用性：当前离线 %d 台。离线期间不要把最后一次上报当成仍在运行的利用率。"
        % len(offline_hosts),
        "部署：本报告不读取部署任务日志，不判断是否存在部署残留。Agent 安装只走 /deploy.html 与中心机部署脚本。",
    ]
    if clamped:
        advice_lines.append(
            "保留期：retention_days=%s，本报告实际样本短于请求的自然周期，结论只覆盖裁剪后的窗口。"
            % retention_days
        )
    if current.get("history_truncated"):
        advice_lines.append(
            "有主机触达单机历史读取上限（%d 行，从窗口起点向后截断），对应指标可能偏窗口前段，已在附录说明，未用其它报告补齐。"
            % REPORT_MAX_ROWS
        )
    advice["paragraphs"] = advice_lines
    sections.append(advice)

    appendix = _section("9. 附录")
    appendix["paragraphs"] = [
        "集群样本数 %s，有样本主机 %s / %s。生成计算时刻（Asia/Shanghai）%s。"
        % (
            current.get("sample_count") or 0,
            current.get("hosts_with_samples") or 0,
            total,
            _fmt_ts(now_ts),
        ),
        "实际查询闭区间（整数秒）：%s～%s。请求半开窗口：%s。"
        % (_fmt_ts(actual_from), _fmt_ts(actual_to_inclusive), bounds["range_text"]),
        "单机读取上限 %d 行。%s"
        % (
            REPORT_MAX_ROWS,
            "本次有主机达到上限。" if current.get("history_truncated") else "本次没有主机达到上限。",
        ),
        "全量主机指标见下载的排行 CSV。下表为窗口内一览（周报、月报与日报同一口径，不另用日文件拼接）。",
    ]
    if clamped:
        appendix["notes"].append(
            "clamped=true，retention_days=%s，实际起点 %s。"
            % (retention_days, _fmt_ts(actual_from))
        )
    preview_hosts = hosts[:100]
    _add_table(
        appendix,
        ["地址", "资源组", "归类", "CPU 平均", "CPU 繁忙", "加速卡", "磁盘最高", "样本"],
        [
            [
                _host_label(h),
                h.get("group_name") or "未分组",
                next(c["pole"] for c in classified if c["host"] is h),
                _fmt_num(_metric(h, "cpu_percent"), "avg", 1, "%"),
                _fmt_busy(_metric(h, "cpu_percent")),
                (
                    _fmt_num(_metric(h, "accel_util_avg"), "avg", 1, "%")
                    if host_has_accel(h)
                    else "无加速卡"
                ),
                _fmt_num(_metric(h, "disk_percent"), "max", 1, "%"),
                str(h.get("sample_count") or 0),
            ]
            for h in preview_hosts
        ]
        or [["样本不足", "样本不足", "样本不足", "样本不足", "样本不足", "样本不足", "样本不足", "样本不足"]],
    )
    if len(hosts) > len(preview_hosts):
        appendix["notes"].append("附录表格仅列出前 100 台，其余见 CSV。")
    sections.append(appendix)

    rankings: List[Dict[str, Any]] = []
    for item in classified:
        host = item["host"]
        cpu = _metric(host, "cpu_percent")
        accel = _metric(host, "accel_util_avg")
        mem = _metric(host, "mem_percent")
        disk = _metric(host, "disk_percent")
        mount = mounts.get(str(host.get("host_id")))
        rankings.append(
            {
                "host_id": host.get("host_id") or "",
                "address": host.get("address") or "",
                "hostname": host.get("hostname") or "",
                "group_name": host.get("group_name") or "",
                "online": 1 if host.get("online") else 0,
                "pole": item["pole"],
                "pole_reason": item["pole_reason"],
                "cpu_avg": cpu.get("avg"),
                "cpu_p95": cpu.get("p95"),
                "cpu_busy_ratio": cpu.get("busy_ratio"),
                "cpu_samples": _sample_count(cpu),
                "accel_count": host.get("accel_count") or 0,
                "accel_summary": host.get("accel_summary") or "",
                "accel_avg": accel.get("avg"),
                "accel_p95": accel.get("p95"),
                "accel_busy_ratio": accel.get("busy_ratio"),
                "accel_samples": _sample_count(accel),
                "mem_avg": mem.get("avg"),
                "mem_p95": mem.get("p95"),
                "disk_avg": disk.get("avg"),
                "disk_p95": disk.get("p95"),
                "disk_max": disk.get("max"),
                "disk_samples": _sample_count(disk),
                "fullest_mount": (mount or {}).get("mount") or "",
                "fullest_mount_percent": (mount or {}).get("percent"),
            }
        )

    markdown = render_markdown(title, sections)
    doc = {
        "period": bounds["period"],
        "label": bounds["label"],
        "title": title,
        "timezone": "Asia/Shanghai",
        "requested_from_ts": bounds["from_ts"],
        "requested_to_ts": bounds["to_ts"],
        "compare_from_ts": bounds["compare_from_ts"],
        "compare_to_ts": bounds["compare_to_ts"],
        "range_text": bounds["range_text"],
        "compare_range_text": bounds["compare_range_text"],
        "clamped": clamped,
        "retention_days": retention_days,
        "actual_from_ts": actual_from,
        "actual_to_ts_inclusive": actual_to_inclusive,
        "busy_thresholds": busy,
        "rules": {
            "rollup": "sample_weighted",
            "idle_avg_lt": IDLE_AVG_LT,
            "idle_busy_lt": IDLE_BUSY_LT,
            "high_busy_gte": HIGH_BUSY_GTE,
            "high_p95_gte": HIGH_P95_GTE,
            "min_samples": min_samples,
            "disk_warn": disk_warn,
            "disk_critical": disk_critical,
            "poles": "cpu_and_accel_only",
        },
        "generated_at": now_ts,
        "sample_count": current.get("sample_count") or 0,
        "history_truncated": bool(current.get("history_truncated")),
        "sections": sections,
        "rankings": rankings,
        "markdown": markdown,
    }
    return None, doc


def rankings_csv(rankings: List[Dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=RANKING_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rankings:
        writer.writerow(row)
    return buf.getvalue()


def _parse_as_of(raw: Any) -> Tuple[Optional[int], Optional[str]]:
    if raw is None or raw == "":
        return None, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, "as_of 必须是 Unix 秒"


def handle_report_preview(storage: Storage, query: str = "") -> Tuple[int, Dict[str, Any]]:
    qs = parse_qs(query or "")
    period = ((qs.get("period") or ["day"])[0] or "day").strip()
    as_of, err = _parse_as_of((qs.get("as_of") or [None])[0])
    if err:
        return 400, {"ok": False, "error": err}
    try:
        bounds = report_period_bounds(period, now=as_of)
    except ValueError as exc:
        return 400, {"ok": False, "error": str(exc)}
    now_ts = int(as_of if as_of is not None else datetime.now(SHANGHAI).timestamp())
    win_err, window = _query_window(storage, bounds["from_ts"], bounds["to_ts"], now_ts)
    return 200, {
        "ok": True,
        "period": bounds["period"],
        "label": bounds["label"],
        "range_text": bounds["range_text"],
        "compare_range_text": bounds["compare_range_text"],
        "requested_from_ts": bounds["from_ts"],
        "requested_to_ts": bounds["to_ts"],
        "retention_days": int(storage.retention_days or 7),
        "clamped": bool(window.get("clamped")) if window else False,
        "window_error": win_err,
        "actual_from_ts": window.get("from_ts") if window else None,
        "actual_to_ts_inclusive": window.get("to_ts") if window else None,
    }


def handle_list_reports(storage: Storage, query: str = "") -> Tuple[int, Dict[str, Any]]:
    qs = parse_qs(query or "")
    raw_limit = (qs.get("limit") or ["50"])[0]
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError):
        return 400, {"ok": False, "error": "limit 必须是整数"}
    return 200, {"ok": True, "reports": storage.list_diagnostic_reports(limit=limit)}


def handle_get_report(storage: Storage, report_id: int) -> Tuple[int, Dict[str, Any]]:
    row = storage.get_diagnostic_report(report_id)
    if not row:
        return 404, {"ok": False, "error": "报告不存在"}
    return 200, {"ok": True, "report": row}


def handle_report_markdown(storage: Storage, report_id: int) -> Tuple[int, Any]:
    row = storage.get_diagnostic_report(report_id)
    if not row:
        return 404, {"ok": False, "error": "报告不存在"}
    filename = "diagnostic-%s-%s.md" % (row.get("period_type") or "report", row["id"])
    return 200, {"markdown": row.get("markdown") or "", "filename": filename}


def handle_report_csv(storage: Storage, report_id: int) -> Tuple[int, Any]:
    row = storage.get_diagnostic_report(report_id)
    if not row:
        return 404, {"ok": False, "error": "报告不存在"}
    payload = row.get("payload") or {}
    text = rankings_csv(payload.get("rankings") or [])
    filename = "diagnostic-%s-%s-rankings.csv" % (
        row.get("period_type") or "report",
        row["id"],
    )
    return 200, {"csv": text, "filename": filename}


def handle_generate_report(
    storage: Storage,
    body: bytes = b"",
    query: str = "",
    busy_thresholds: Optional[Dict[str, float]] = None,
    anomaly_thresholds: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Dict[str, Any]]:
    data: Dict[str, Any] = {}
    if body:
        try:
            parsed = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            return 400, {"ok": False, "error": "请求体必须是合法 JSON"}
        if not isinstance(parsed, dict):
            return 400, {"ok": False, "error": "JSON 根节点必须是对象"}
        data = parsed
    qs = parse_qs(query or "")
    period = str(data.get("period") or (qs.get("period") or ["day"])[0] or "day").strip()
    as_of_raw = data.get("as_of", (qs.get("as_of") or [None])[0])
    as_of, err = _parse_as_of(as_of_raw)
    if err:
        return 400, {"ok": False, "error": err}
    build_err, doc = build_report(
        storage,
        period,
        now=as_of,
        busy_thresholds=busy_thresholds,
        anomaly_thresholds=anomaly_thresholds,
    )
    if build_err or not doc:
        return 400, {"ok": False, "error": build_err or "无法生成报告"}
    saved = storage.save_diagnostic_report(
        period_type=doc["period"],
        title=doc["title"],
        requested_from_ts=int(doc["requested_from_ts"]),
        requested_to_ts=int(doc["requested_to_ts"]),
        clamped=bool(doc["clamped"]),
        markdown=doc["markdown"],
        payload=doc,
    )
    return 200, {"ok": True, "report": saved}
