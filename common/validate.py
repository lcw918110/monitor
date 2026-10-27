"""公共工具：配置校验。"""

from typing import Any, Dict, List, Tuple


def validate_center_config(cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    port = cfg.get("port")
    try:
        port_i = int(port)
        if not (1 <= port_i <= 65535):
            errors.append("port 必须在 1-65535")
    except (TypeError, ValueError):
        errors.append("port 必须是整数")

    offline = cfg.get("offline_seconds", 90)
    try:
        if int(offline) < 15:
            errors.append("offline_seconds 建议 ≥ 15")
    except (TypeError, ValueError):
        errors.append("offline_seconds 必须是整数")

    retention = cfg.get("retention_days", 7)
    try:
        if int(retention) < 0:
            errors.append("retention_days 不能为负")
    except (TypeError, ValueError):
        errors.append("retention_days 必须是整数")

    anomaly = cfg.get("anomaly")
    if anomaly is not None and not isinstance(anomaly, dict):
        errors.append("anomaly 必须是对象")

    period_util = cfg.get("period_util")
    if period_util is not None and not isinstance(period_util, dict):
        errors.append("period_util 必须是对象")

    report = cfg.get("report")
    if report is not None and not isinstance(report, dict):
        errors.append("report 必须是对象")
    elif isinstance(report, dict):
        ranges = {
            "idle_avg_lt": (0.0, 100.0),
            "idle_busy_lt": (0.0, 1.0),
            "high_busy_gte": (0.0, 1.0),
            "high_p95_gte": (0.0, 100.0),
        }
        for key, (low, high) in ranges.items():
            if key not in report or report.get(key) in (None, ""):
                continue
            raw = report.get(key)
            if isinstance(raw, bool):
                errors.append("report.%s 必须是数字" % key)
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                errors.append("report.%s 必须是数字" % key)
                continue
            if not (low <= value <= high):
                errors.append("report.%s 必须在 %s-%s" % (key, low, high))
        if "min_samples" in report and report.get("min_samples") not in (None, ""):
            raw_count = report.get("min_samples")
            if isinstance(raw_count, bool):
                errors.append("report.min_samples 必须是整数")
            else:
                try:
                    count = int(raw_count)
                except (TypeError, ValueError):
                    errors.append("report.min_samples 必须是整数")
                else:
                    if isinstance(raw_count, float) and abs(raw_count - count) > 1e-9:
                        errors.append("report.min_samples 必须是整数")
                    elif not (1 <= count <= 1000000):
                        errors.append("report.min_samples 必须在 1-1000000")

    schedule = cfg.get("report_schedule")
    if schedule is not None:
        if not isinstance(schedule, dict):
            errors.append("report_schedule 必须是对象")
        else:
            for key in ("daily", "weekly", "monthly"):
                slot = schedule.get(key)
                if slot is None:
                    continue
                label = "report_schedule.%s" % key
                if not isinstance(slot, dict):
                    errors.append("%s 必须是对象" % label)
                    continue
                for field, lo, hi in (("hour", 0, 23), ("minute", 0, 59)):
                    if field not in slot or slot[field] in (None, ""):
                        continue
                    try:
                        value = int(slot[field])
                    except (TypeError, ValueError):
                        errors.append("%s.%s 必须是整数" % (label, field))
                        continue
                    if not (lo <= value <= hi):
                        errors.append("%s.%s 必须在 %d-%d" % (label, field, lo, hi))

    return len(errors) == 0, errors


def validate_agent_config(cfg: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    url = cfg.get("center_url") or ""
    if not isinstance(url, str) or not url.startswith("http"):
        errors.append("center_url 必须以 http:// 或 https:// 开头")
    elif "/api/v1/metrics" not in url:
        errors.append("center_url 应指向 /api/v1/metrics")

    interval = cfg.get("interval_seconds", 15)
    try:
        if int(interval) < 5:
            errors.append("interval_seconds 不能小于 5")
    except (TypeError, ValueError):
        errors.append("interval_seconds 必须是整数")

    from common.host_type import normalize_host_type

    raw = cfg.get("host_type") or "auto"
    host_type = normalize_host_type(raw)
    # 允许历史值写入配置文件，归一化后必须合法
    if host_type not in ("auto", "cpu", "gpu"):
        errors.append("host_type 仅支持 auto|cpu|gpu（旧值 app→cpu、npu→gpu）")
    else:
        cfg["host_type"] = host_type

    return len(errors) == 0, errors
