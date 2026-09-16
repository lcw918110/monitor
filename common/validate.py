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
