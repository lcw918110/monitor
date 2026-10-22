"""监测台给人看的状态说法。

原始 ``health`` / ``note`` 仍留在采集数据里，这里只生成界面和异常说明用的中文。
规则与 ``center/static/ux.js`` 的 ``accelHealthLabel`` 保持一致。
"""

from __future__ import annotations


def accel_health_label(health: object, note: object = "") -> str:
    """把卡的 health / note 说成白话。空健康是「未上报」，不是正常。"""
    text = str(note or "")
    raw = str(health or "").strip()
    upper = raw.upper()
    if "不在 PATH" in text or "未安装" in text:
        return "工具不在 PATH"
    if "驱动" in text:
        return "驱动缺失"
    if "无响应" in text or "rev ff" in text.lower() or "硬件" in text:
        return "硬件异常"
    if not raw or upper in ("UNKNOWN", "NONE", "N/A", "NULL"):
        return "未上报"
    if upper in ("OK", "GOOD", "HEALTHY", "NORMAL", "PASS"):
        return "正常"
    if upper in ("CRITICAL", "ALARM"):
        return "硬件异常"
    return "异常"
