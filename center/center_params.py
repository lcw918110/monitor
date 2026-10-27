"""中心参数目录：默认值、说明、校验，以及写回 config/center.json。

设置页按这里的分组展示。未出现在配置文件里的键用内置默认。
管理 Token 和 Webhook 不在这份目录里，它们仍只进 admin_settings。
"""

from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any, Dict, List, Optional, Tuple

from center.report_schedule import merge_report_schedule, normalize_report_schedule
from center.reports import (
    HIGH_BUSY_GTE,
    HIGH_P95_GTE,
    IDLE_AVG_LT,
    IDLE_BUSY_LT,
    REPORT_MIN_SAMPLES,
)


# 旧 center.json 只写了 npu_* 时，对应到现在的 accel_*。两边都写时以 accel_* 为准。
ANOMALY_ALIASES = {
    "accel_util_warn_percent": "npu_util_warn_percent",
    "accel_temp_warn_c": "npu_temp_warn_c",
    "accel_temp_critical_c": "npu_temp_critical_c",
    "accel_mem_warn_percent": "npu_mem_warn_percent",
    "accel_mem_critical_percent": "npu_mem_critical_percent",
}

_RELATIONS = (
    (
        "anomaly.cpu_warn_percent",
        "anomaly.cpu_critical_percent",
        "CPU 偏高线必须低于异常线",
    ),
    (
        "anomaly.mem_warn_percent",
        "anomaly.mem_critical_percent",
        "内存偏高线必须低于异常线",
    ),
    (
        "anomaly.disk_warn_percent",
        "anomaly.disk_critical_percent",
        "磁盘偏高线必须低于异常线",
    ),
    (
        "anomaly.accel_temp_warn_c",
        "anomaly.accel_temp_critical_c",
        "加速卡温度偏高线必须低于异常线",
    ),
    (
        "anomaly.accel_mem_warn_percent",
        "anomaly.accel_mem_critical_percent",
        "加速卡显存偏高线必须低于异常线",
    ),
)


def _field(key, label, description, kind, default, **extra):
    item = {
        "key": key,
        "label": label,
        "description": description,
        "kind": kind,
        "default": default,
        "restart": bool(extra.pop("restart", False)),
        "unit": extra.pop("unit", ""),
    }
    item.update(extra)
    return item


def _num(key, label, description, default, unit, low, high, step=1, kind="number"):
    return _field(
        key,
        label,
        description,
        kind,
        default,
        unit=unit,
        min=low,
        max=high,
        step=step,
    )


GROUPS: List[Dict[str, Any]] = [
    {
        "id": "retention",
        "anchor": "lead",
        "title": "保留与清理",
        "description": "决定历史上报留多久、多久算离线，以及清理跑多勤。常用的保留天数在最前。",
        "fields": [
            _num(
                "retention_days",
                "历史上报保留天数",
                "用途：控制 metrics_history 留存。单位：天。影响：超过后只删除历史上报，不删除已生成的诊断报告；改小会让更早的日/周/月窗口被裁短，报告抬头写 clamped。填 0 表示不按天数删除，时段查询和诊断报告也不再按 7 天裁短。",
                7,
                "天",
                0,
                3650,
            ),
            _num(
                "offline_seconds",
                "离线判定",
                "用途：多久没有上报就把主机当成离线。单位：秒。影响：监测台上的在线/离线显示，以及诊断报告里离线主机不计入基本不用。建议大于采样间隔，且至少 15 秒。",
                90,
                "秒",
                15,
                86400,
            ),
            _num(
                "cleanup_interval_seconds",
                "清理间隔",
                "用途：两次扫描并删除过期历史上报之间的间隔。单位：秒。影响：只改变清理频率，不改变保留天数。至少 60 秒。",
                3600,
                "秒",
                60,
                7 * 86400,
            ),
        ],
    },
    {
        "id": "anomaly",
        "anchor": "lead",
        "title": "异常与负载着色",
        "description": "只影响监测台黄/红和今日关注，不新增指标，也不发实时告警。负载每核阈值放在最前。",
        "fields": [
            _num(
                "anomaly.cpu_load_per_core_warn",
                "负载每核偏高线",
                "用途：load1、load5 除以 CPU 核数，达到这个倍数就标成偏高（黄）并进入今日关注。单位：每核负载，不是百分数。影响：只改着色；load15 仍只展示。默认 2.0。",
                2.0,
                "倍",
                0.1,
                100,
                step=0.1,
            ),
            _num(
                "anomaly.cpu_warn_percent",
                "CPU 偏高线",
                "用途：CPU 利用率达到后标偏高（黄）。单位：%。影响：监测台着色和今日关注。应低于 CPU 异常线。",
                80,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.cpu_critical_percent",
                "CPU 异常线",
                "用途：CPU 利用率达到后标异常（红）。单位：%。影响：监测台着色和今日关注。应高于 CPU 偏高线。",
                95,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.mem_warn_percent",
                "内存偏高线",
                "用途：内存利用率达到后标偏高（黄）。单位：%。影响：监测台着色；诊断报告里「内存偏高」用同一条线。不进入闲忙两极。",
                85,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.mem_critical_percent",
                "内存异常线",
                "用途：内存利用率达到后标异常（红）。单位：%。影响：监测台着色。应高于内存偏高线。",
                95,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.disk_warn_percent",
                "磁盘偏高线",
                "用途：磁盘利用率达到后标偏高（黄）。单位：%。影响：监测台着色，以及诊断报告里磁盘分档的偏低那一档。磁盘满不等于算力忙。",
                85,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.disk_critical_percent",
                "磁盘异常线",
                "用途：磁盘利用率达到后标异常（红），并计入今日关注里的磁盘快满。单位：%。影响：监测台和诊断报告的磁盘临界档。应高于磁盘偏高线。",
                95,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.accel_util_warn_percent",
                "加速卡利用率偏高线",
                "用途：单卡利用率达到后标偏高。单位：%。影响：监测台加速卡着色。没有单独的异常档。旧键 npu_util_warn_percent 仍认；这里改的是 accel 键，恢复默认时两个键一起回到内置值。",
                95,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.accel_temp_warn_c",
                "加速卡温度偏高线",
                "用途：卡温度达到后标偏高（黄）。单位：℃。影响：监测台温度着色。应低于温度异常线。旧键 npu_temp_warn_c 仍认。",
                85,
                "℃",
                0,
                150,
            ),
            _num(
                "anomaly.accel_temp_critical_c",
                "加速卡温度异常线",
                "用途：卡温度达到后标异常（红）。单位：℃。影响：监测台温度着色。应高于温度偏高线。旧键 npu_temp_critical_c 仍认。",
                95,
                "℃",
                0,
                150,
            ),
            _num(
                "anomaly.accel_mem_warn_percent",
                "加速卡显存偏高线",
                "用途：卡显存占用达到后标偏高（黄）。单位：%。影响：监测台显存着色。应低于显存异常线。旧键 npu_mem_warn_percent 仍认。",
                90,
                "%",
                0,
                100,
            ),
            _num(
                "anomaly.accel_mem_critical_percent",
                "加速卡显存异常线",
                "用途：卡显存占用达到后标异常（红）。单位：%。影响：监测台显存着色。应高于显存偏高线。旧键 npu_mem_critical_percent 仍认。",
                98,
                "%",
                0,
                100,
            ),
        ],
    },
    {
        "id": "report",
        "anchor": "lead",
        "title": "诊断报告阈值",
        "description": "只影响日/周/月诊断报告的闲忙划分。改时段繁忙线不会改这里。已入库的旧报告不自动重算。",
        "fields": [
            _num(
                "report.idle_avg_lt",
                "基本不用 · 平均利用率上限",
                "用途：CPU 与加速卡「基本不用」的平均利用率必须低于这条（不含等于）。单位：%。影响：诊断报告的空闲名单和抬头口径。默认 5，即平均 <5%。改 period_util 不会带动这条。",
                IDLE_AVG_LT,
                "%",
                0,
                100,
                step=0.1,
            ),
            _num(
                "report.idle_busy_lt",
                "基本不用 · 繁忙占比上限",
                "用途：基本不用还要求繁忙占比低于这条（不含等于）。单位：比例，不是百分数；0.05 表示 5%。影响：CPU 与有卡时的加速卡都要同时低于平均上限和这条。默认 0.05。",
                IDLE_BUSY_LT,
                "比例",
                0,
                1,
                step=0.01,
            ),
            _num(
                "report.high_busy_gte",
                "高负荷 · 繁忙占比下限",
                "用途：样本足够时，CPU 或加速卡繁忙占比达到这条即算高负荷。单位：比例；0.40 表示 40%。影响：诊断报告高负荷名单。与 P95 线是「或」的关系。默认 0.40。",
                HIGH_BUSY_GTE,
                "比例",
                0,
                1,
                step=0.01,
            ),
            _num(
                "report.high_p95_gte",
                "高负荷 · P95 下限",
                "用途：样本足够时，CPU 或加速卡 P95 达到这条也算高负荷。单位：%。影响：诊断报告高负荷名单，用来抓住尖峰。默认 85。",
                HIGH_P95_GTE,
                "%",
                0,
                100,
                step=0.1,
            ),
            _num(
                "report.min_samples",
                "闲忙最少样本数",
                "用途：低于这个样本数不判高负荷，也不判基本不用，报告写样本不足。单位：条。影响：日/周/月报告的闲忙分类。默认 10。不编造缺数。",
                REPORT_MIN_SAMPLES,
                "条",
                1,
                1000000,
            ),
        ],
    },
    {
        "id": "period",
        "anchor": "lead",
        "title": "时段利用率",
        "description": "时段统计和诊断报告共用的「一条样本算不算繁忙」。查询参数仍可临时覆盖。内存和磁盘默认不计算繁忙占比。",
        "fields": [
            _num(
                "period_util.busy_cpu_percent",
                "CPU 繁忙线",
                "用途：时段统计里 CPU 利用率大于等于这条的样本计入繁忙占比。单位：%。影响：时段页和诊断报告里的 CPU 繁忙占比。不改变高负荷/基本不用那两条报告线。填 -1 表示不计算 CPU 繁忙占比。默认 80。",
                80,
                "%",
                -1,
                100,
            ),
            _num(
                "period_util.busy_accel_percent",
                "加速卡繁忙线",
                "用途：加速卡平均利用率大于等于这条的样本计入繁忙占比。单位：%。影响：时段页和诊断报告里的加速卡繁忙占比。不改变报告的空闲/高负荷线。填 -1 表示不计算。默认 80。",
                80,
                "%",
                -1,
                100,
            ),
            _field(
                "period_util.busy_mem_percent",
                "内存繁忙线",
                "用途：可选。单位：%。影响：填了之后，时段统计才给内存计算繁忙占比。留空表示不计算（默认）。内存仍然不进入诊断报告的闲忙两极。填 -1 表示显式关闭。",
                "optional_number",
                None,
                unit="%",
                min=-1,
                max=100,
                step=1,
                placeholder="留空表示不计算",
            ),
            _field(
                "period_util.busy_disk_percent",
                "磁盘繁忙线",
                "用途：可选。单位：%。影响：填了之后，时段统计才给磁盘计算繁忙占比。留空表示不计算（默认）。磁盘满不等于算力忙，也不进入闲忙两极。填 -1 表示显式关闭。",
                "optional_number",
                None,
                unit="%",
                min=-1,
                max=100,
                step=1,
                placeholder="留空表示不计算",
            ),
        ],
    },
    {
        "id": "schedule",
        "anchor": "lead",
        "title": "报告生成时刻",
        "description": "中心进程按上海时间在配置的那一分钟生成上一完整周期。错过不补。周报固定周一，月报固定每月 1 日，这两天不能改。",
        "fields": [
            _field(
                "report_schedule.enabled",
                "启用定时生成",
                "用途：总开关。影响：关掉后日、周、月都不再自动生成；页面上仍可手动生成。保存后当前进程按新开关检查，不必为此重启。",
                "bool",
                True,
            ),
            _field(
                "report_schedule.daily.enabled",
                "日报",
                "用途：是否在每天的指定时刻生成刚结束的那一天。影响：只关日报，周报和月报仍按各自开关。",
                "bool",
                True,
            ),
            _num(
                "report_schedule.daily.hour",
                "日报小时",
                "用途：日报触发的钟点，上海时间。单位：时（0–23）。影响：只在这一分钟、进程还活着时生成，错过不补。默认 0 点。",
                0,
                "时",
                0,
                23,
            ),
            _num(
                "report_schedule.daily.minute",
                "日报分钟",
                "用途：日报触发的分钟。单位：分（0–59）。影响：与小时合成触发时刻。默认 10 分，即 00:10。",
                10,
                "分",
                0,
                59,
            ),
            _field(
                "report_schedule.weekly.enabled",
                "周报",
                "用途：是否在每周一的指定时刻生成刚结束的那一周。影响：只关周报。星期几固定为周一。",
                "bool",
                True,
            ),
            _num(
                "report_schedule.weekly.hour",
                "周报小时",
                "用途：周报触发的钟点，上海时间，仅周一。单位：时（0–23）。影响：错过该分钟不补。默认 0 点。",
                0,
                "时",
                0,
                23,
            ),
            _num(
                "report_schedule.weekly.minute",
                "周报分钟",
                "用途：周报触发的分钟。单位：分（0–59）。影响：与小时合成触发时刻。默认 20 分，即周一 00:20。",
                20,
                "分",
                0,
                59,
            ),
            _field(
                "report_schedule.monthly.enabled",
                "月报",
                "用途：是否在每月 1 日的指定时刻生成刚结束的那一月。影响：只关月报。日期固定为 1 日。",
                "bool",
                True,
            ),
            _num(
                "report_schedule.monthly.hour",
                "月报小时",
                "用途：月报触发的钟点，上海时间，仅每月 1 日。单位：时（0–23）。影响：错过该分钟不补。默认 0 点。",
                0,
                "时",
                0,
                23,
            ),
            _num(
                "report_schedule.monthly.minute",
                "月报分钟",
                "用途：月报触发的分钟。单位：分（0–59）。影响：与小时合成触发时刻。默认 30 分，即每月 1 日 00:30。",
                30,
                "分",
                0,
                59,
            ),
        ],
    },
    {
        "id": "security",
        "anchor": "security",
        "title": "Agent 上报 Token",
        "description": "与上面的管理 Token、报告推送不是同一把钥匙。管理 Token 和 Webhook 只存在数据库里，不写进 center.json。",
        "fields": [
            _field(
                "token",
                "Agent 上报 Token",
                "用途：Agent 上报口令。影响：只校验 POST /api/v1/metrics，不能用来改设置，也不是网页登录。留空的输入框表示不修改；要清空请点恢复默认，清空后上报不再校验。单位：无。保存后接口不回显明文。写在 center.json。",
                "secret",
                "",
                placeholder="留空表示不修改",
            ),
        ],
    },
    {
        "id": "service",
        "anchor": "tail",
        "title": "服务与部署",
        "description": "这些项不常改。保存后写入配置文件，当前进程的监听地址、端口和数据库目录要重启 monitor-center 才换。这不是升级。",
        "fields": [
            _field(
                "host",
                "监听地址",
                "用途：中心 HTTP 绑定的地址。单位：无。影响：重启后才改绑定。默认 0.0.0.0 表示所有网卡。当前进程不会因为保存而改端口。",
                "string",
                "0.0.0.0",
                restart=True,
                max_length=253,
            ),
            _num(
                "port",
                "监听端口",
                "用途：中心 HTTP 端口。单位：端口号。影响：写入配置后要重启才换端口，当前页面仍停在原来的端口上。",
                8080,
                "端口",
                1,
                65535,
                restart=True,
            ),
            _field(
                "data_dir",
                "数据目录",
                "用途：monitor.db 所在目录。单位：路径。影响：重启后新进程使用这个目录，不会把现有库搬走。改错会导致看起来像「库没了」。",
                "string",
                "data",
                restart=True,
                max_length=400,
            ),
            _field(
                "deploy.python_bundles",
                "便携 Python 目录",
                "用途：部署 Agent 时使用的便携 CPython 包目录。单位：路径。影响：只影响之后的部署任务，不改采集指标。留空则自动选择：优先 /opt/monitor/vendor/python，否则用仓库里的 vendor/python。环境变量 MONITOR_PYTHON_BUNDLES 优先于本项。",
                "string",
                "",
                max_length=400,
                placeholder="留空表示自动选择",
                empty_clears=True,
            ),
        ],
    },
]


def _index_fields() -> Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]]:
    found: Dict[str, Tuple[Dict[str, Any], Dict[str, Any]]] = {}
    for group in GROUPS:
        for field in group["fields"]:
            key = field["key"]
            if key in found:
                raise RuntimeError("重复的中心参数 %s" % key)
            found[key] = (group, field)
    return found


FIELDS = _index_fields()


def default_center_config() -> Dict[str, Any]:
    """与进程内置默认一致。文件里没写的键用这里的值。"""
    anomaly = {
        "cpu_warn_percent": 80,
        "cpu_critical_percent": 95,
        "cpu_load_per_core_warn": 2.0,
        "mem_warn_percent": 85,
        "mem_critical_percent": 95,
        "disk_warn_percent": 85,
        "disk_critical_percent": 95,
        "accel_util_warn_percent": 95,
        "accel_temp_warn_c": 85,
        "accel_temp_critical_c": 95,
        "accel_mem_warn_percent": 90,
        "accel_mem_critical_percent": 98,
    }
    return {
        "host": "0.0.0.0",
        "port": 8080,
        "data_dir": "data",
        "token": "",
        "offline_seconds": 90,
        "retention_days": 7,
        "cleanup_interval_seconds": 3600,
        "anomaly": anomaly,
        "period_util": {
            "busy_cpu_percent": 80,
            "busy_accel_percent": 80,
        },
        "report": {
            "idle_avg_lt": IDLE_AVG_LT,
            "idle_busy_lt": IDLE_BUSY_LT,
            "high_busy_gte": HIGH_BUSY_GTE,
            "high_p95_gte": HIGH_P95_GTE,
            "min_samples": REPORT_MIN_SAMPLES,
        },
        "report_schedule": merge_report_schedule(None),
    }


def _apply_anomaly_aliases(merged: Dict[str, Any], user_anomaly: Dict[str, Any]) -> None:
    for new_key, old_key in ANOMALY_ALIASES.items():
        if new_key in user_anomaly:
            continue
        if old_key not in user_anomaly or user_anomaly.get(old_key) is None:
            continue
        merged[new_key] = user_anomaly[old_key]


def merge_center_config(user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """把配置文件叠到内置默认上。缺省的 report_schedule 用默认时刻，不改 token。"""
    cfg = default_center_config()
    if not user:
        return cfg
    if not isinstance(user, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    data = dict(user)
    user_anomaly = data.pop("anomaly", None)
    user_period = data.pop("period_util", None)
    user_schedule = data.pop("report_schedule", None)
    user_report = data.pop("report", None)
    data.pop("alerts", None)
    cfg.update(data)
    if isinstance(user_anomaly, dict):
        anomaly = dict(cfg.get("anomaly") or {})
        anomaly.update(user_anomaly)
        _apply_anomaly_aliases(anomaly, user_anomaly)
        cfg["anomaly"] = anomaly
    elif user_anomaly is not None:
        cfg["anomaly"] = user_anomaly
    if isinstance(user_period, dict):
        period = dict(cfg.get("period_util") or {})
        period.update(user_period)
        cfg["period_util"] = period
    elif user_period is not None:
        cfg["period_util"] = user_period
    if isinstance(user_report, dict):
        report = dict(cfg.get("report") or {})
        report.update(user_report)
        cfg["report"] = report
    elif user_report is not None:
        cfg["report"] = user_report
    if user_schedule is None:
        cfg["report_schedule"] = merge_report_schedule(None)
    else:
        cfg["report_schedule"] = merge_report_schedule(user_schedule)
    return cfg


def read_user_config(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return data


def write_user_config(path: str, doc: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(doc, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)


def period_util_thresholds(period_util: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    from center.storage import DEFAULT_BUSY_THRESHOLDS

    thresholds = dict(DEFAULT_BUSY_THRESHOLDS)
    cfg = period_util or {}
    mapping = {
        "busy_cpu_percent": "cpu_percent",
        "busy_accel_percent": "accel_util_avg",
        "busy_mem_percent": "mem_percent",
        "busy_disk_percent": "disk_percent",
    }
    for cfg_key, metric in mapping.items():
        raw = cfg.get(cfg_key)
        if raw is None or raw == "" or isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value < 0:
            thresholds.pop(metric, None)
        else:
            thresholds[metric] = value
    return thresholds


def _lookup(doc: Any, dotted: str) -> Tuple[bool, Any]:
    current = doc
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return False, None
        current = current[part]
    return True, current


def _delete_path(doc: Dict[str, Any], dotted: str) -> None:
    parts = dotted.split(".")
    stack: List[Tuple[Dict[str, Any], str]] = []
    current: Any = doc
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        stack.append((current, part))
        current = current[part]
    if isinstance(current, dict):
        current.pop(parts[-1], None)
    for parent, part in reversed(stack):
        child = parent.get(part)
        if isinstance(child, dict) and not child:
            parent.pop(part, None)
        else:
            break


def _set_path(doc: Dict[str, Any], dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    current = doc
    for part in parts[:-1]:
        nxt = current.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            current[part] = nxt
        current = nxt
    current[parts[-1]] = value


def _as_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value in (0, 1) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return None


def _as_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _is_integral_field(field: Dict[str, Any]) -> bool:
    key = field["key"]
    if key.endswith(".hour") or key.endswith(".minute"):
        return True
    return key in (
        "retention_days",
        "offline_seconds",
        "cleanup_interval_seconds",
        "port",
        "report.min_samples",
    )


def _coerce_field(field: Dict[str, Any], raw: Any) -> Tuple[Optional[str], Any]:
    kind = field["kind"]
    label = field["label"]
    if kind == "bool":
        parsed = _as_bool(raw)
        if parsed is None:
            return "%s 只能是 true 或 false" % label, None
        return None, parsed
    if kind == "secret":
        if not isinstance(raw, str):
            return "%s 必须是字符串" % label, None
        text = raw.strip()
        if text == "":
            return "留空不会修改%s，清空请恢复默认" % label, None
        if any(ch.isspace() for ch in text):
            return "%s 不能包含空白" % label, None
        if len(text) > 256:
            return "%s 不能超过 256 个字符" % label, None
        return None, text
    if kind == "string":
        if not isinstance(raw, str):
            return "%s 必须是字符串" % label, None
        text = raw.strip()
        if field.get("empty_clears") and text == "":
            return None, None
        if text == "":
            return "%s 不能为空" % label, None
        if any(ch in text for ch in ("\n", "\r", "\x00")):
            return "%s 不能包含换行" % label, None
        limit = int(field.get("max_length") or 400)
        if len(text) > limit:
            return "%s 过长" % label, None
        return None, text
    if kind in ("number", "optional_number"):
        if kind == "optional_number" and raw in ("", None):
            return None, None
        number = _as_number(raw)
        if number is None:
            return "%s 必须是数字" % label, None
        low = field.get("min")
        high = field.get("max")
        if low is not None and number < float(low):
            return "%s 不能小于 %s" % (label, _trim_number(low)), None
        if high is not None and number > float(high):
            return "%s 不能大于 %s" % (label, _trim_number(high)), None
        if _is_integral_field(field):
            if abs(number - round(number)) > 1e-9:
                return "%s 必须是整数" % label, None
            return None, int(round(number))
        return None, float(number)
    return "无法识别的参数类型", None


def _trim_number(value: Any) -> str:
    number = float(value)
    if abs(number - round(number)) < 1e-9:
        return str(int(round(number)))
    return ("%.4f" % number).rstrip("0").rstrip(".")


def _mask_secret(text: str) -> str:
    if not text:
        return ""
    if len(text) < 8:
        return "已设置"
    return "已设置，末四位 " + text[-4:]


def _configured(user_doc: Dict[str, Any], field: Dict[str, Any]) -> bool:
    present, _value = _lookup(user_doc, field["key"])
    if present:
        return True
    if field["key"] in ANOMALY_ALIASES:
        alias = "anomaly." + ANOMALY_ALIASES[field["key"]]
        alias_present, _alias_value = _lookup(user_doc, alias)
        if alias_present:
            return True
    return False


def _relation_error(effective: Dict[str, Any], touched: List[str]) -> Optional[str]:
    touched_set = set(touched)
    for low_key, high_key, message in _RELATIONS:
        if low_key not in touched_set and high_key not in touched_set:
            continue
        low_present, low_value = _lookup(effective, low_key)
        high_present, high_value = _lookup(effective, high_key)
        if not low_present or not high_present:
            continue
        low_number = _as_number(low_value)
        high_number = _as_number(high_value)
        if low_number is None or high_number is None:
            continue
        if low_number >= high_number:
            return message
    return None


def _alias_keys(field_key: str) -> List[str]:
    name = field_key.split(".")[-1]
    old = ANOMALY_ALIASES.get(name)
    if not old:
        return []
    return ["anomaly." + old]


def _group_by_id(group_id: str) -> Optional[Dict[str, Any]]:
    for group in GROUPS:
        if group["id"] == group_id:
            return group
    return None


def _keys_for_group(group_id: str) -> List[str]:
    group = _group_by_id(group_id)
    if group is None:
        return []
    return [field["key"] for field in group["fields"]]


class CenterRuntime:
    """进程内正在使用的中心配置。保存时写文件，并让报告、着色、清理读到新值。"""

    def __init__(
        self,
        user_doc: Optional[Dict[str, Any]],
        config_path: str,
        storage: Any = None,
        scheduler: Any = None,
        deploy_runner: Any = None,
        cleanup: Any = None,
        project_root: str = "",
    ) -> None:
        self._lock = threading.RLock()
        self.user_doc = copy.deepcopy(user_doc or {})
        self.path = config_path
        self.storage = storage
        self.scheduler = scheduler
        self.deploy_runner = deploy_runner
        self.cleanup = cleanup
        self.project_root = project_root
        self.effective = merge_center_config(self.user_doc)
        self._push_live()

    def anomaly(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.effective.get("anomaly") or {})

    def period_util(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.effective.get("period_util") or {})

    def report_section(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.effective.get("report") or {})

    def agent_token(self) -> str:
        with self._lock:
            return str(self.effective.get("token") or "")

    def busy_thresholds(self) -> Dict[str, float]:
        return period_util_thresholds(self.period_util())

    def public_view(self) -> Dict[str, Any]:
        with self._lock:
            return self._public_view()

    def _public_view(self) -> Dict[str, Any]:
        groups = []
        for group in GROUPS:
            fields = []
            for field in group["fields"]:
                fields.append(self._public_field(field))
            groups.append(
                {
                    "id": group["id"],
                    "anchor": group["anchor"],
                    "title": group["title"],
                    "description": group["description"],
                    "fields": fields,
                }
            )
        return {
            "config_path": self.path,
            "groups": groups,
        }

    def _public_field(self, field: Dict[str, Any]) -> Dict[str, Any]:
        present, _raw = _lookup(self.effective, field["key"])
        configured = _configured(self.user_doc, field)
        view: Dict[str, Any] = {
            "key": field["key"],
            "label": field["label"],
            "description": field["description"],
            "kind": field["kind"],
            "unit": field.get("unit") or "",
            "default": field["default"],
            "using_default": not configured,
            "restart_required": bool(field.get("restart")),
        }
        if field.get("min") is not None:
            view["min"] = field["min"]
        if field.get("max") is not None:
            view["max"] = field["max"]
        if field.get("step") is not None:
            view["step"] = field["step"]
        if field.get("placeholder"):
            view["placeholder"] = field["placeholder"]
        if field["kind"] == "secret":
            secret = str(self.effective.get("token") or "")
            view["value"] = None
            view["configured"] = bool(secret)
            view["hint"] = _mask_secret(secret) if secret else ""
            view["using_default"] = not bool(secret)
            return view
        if field["kind"] == "optional_number":
            user_present, user_value = _lookup(self.user_doc, field["key"])
            if not user_present:
                view["value"] = None
            else:
                view["value"] = user_value
            return view
        if present:
            view["value"] = _lookup(self.effective, field["key"])[1]
        else:
            view["value"] = field["default"]
        return view

    def validate(self, payload: Any) -> Optional[str]:
        """只检查，不写文件。给和管理 Token 同一请求时先挡住非法中心参数。"""
        if not isinstance(payload, dict):
            return "center 必须是对象"
        with self._lock:
            error, _doc, _restart = self._prepare(payload)
            return error

    def apply(self, payload: Any) -> Tuple[int, Dict[str, Any]]:
        if not isinstance(payload, dict):
            return 400, {"ok": False, "error": "center 必须是对象"}
        with self._lock:
            error, new_doc, restart = self._prepare(payload)
            if error:
                return 400, {"ok": False, "error": error}
            if not self.path:
                return 503, {"ok": False, "error": "没有可写入的配置文件"}
            try:
                write_user_config(self.path, new_doc)
            except OSError:
                return 500, {"ok": False, "error": "配置文件写入失败"}
            self.user_doc = new_doc
            self.effective = merge_center_config(new_doc)
            self._push_live()
            return 200, {
                "ok": True,
                "center": self._public_view(),
                "restart_required": restart,
            }

    def _prepare(
        self, payload: Dict[str, Any]
    ) -> Tuple[Optional[str], Dict[str, Any], List[str]]:
        unknown = set(payload) - {"values", "restore", "restore_group", "restore_all", "clear"}
        if unknown:
            return "center 含无法识别的字段", {}, []
        restore_all = payload.get("restore_all", False)
        if "restore_all" in payload and not isinstance(restore_all, bool):
            parsed = _as_bool(restore_all)
            if parsed is None:
                return "restore_all 只能是 true 或 false", {}, []
            restore_all = parsed
        values = payload.get("values", {})
        restore = payload.get("restore", [])
        clear = payload.get("clear", [])
        restore_group = payload.get("restore_group", [])
        if values in (None, ""):
            values = {}
        if not isinstance(values, dict):
            return "values 必须是对象", {}, []
        if isinstance(restore, str):
            restore = [restore]
        if isinstance(clear, str):
            clear = [clear]
        if isinstance(restore_group, str):
            restore_group = [restore_group]
        if not isinstance(restore, list) or not all(isinstance(item, str) for item in restore):
            return "restore 必须是参数键列表", {}, []
        if not isinstance(clear, list) or not all(isinstance(item, str) for item in clear):
            return "clear 必须是参数键列表", {}, []
        if not isinstance(restore_group, list) or not all(
            isinstance(item, str) for item in restore_group
        ):
            return "restore_group 必须是分组 id", {}, []
        if restore_all and (values or restore or clear or restore_group):
            return "全部恢复默认时不要同时提交别的修改", {}, []
        if not restore_all and not values and not restore and not clear and not restore_group:
            return "没有要保存的中心参数", {}, []

        group_keys: List[str] = []
        for group_id in restore_group:
            if _group_by_id(group_id) is None:
                return "未知分组 %s" % group_id, {}, []
            group_keys.extend(_keys_for_group(group_id))

        delete_keys = list(restore) + list(clear) + group_keys
        if restore_all:
            delete_keys = list(FIELDS)
        seen = set()
        for key in list(values) + delete_keys:
            if key in seen and key in values and key in delete_keys:
                return "不能同时修改和恢复 %s" % key, {}, []
            seen.add(key)
        for key in list(values) + delete_keys:
            if key not in FIELDS:
                return "未知参数 %s" % key, {}, []
        for key in clear:
            if FIELDS[key][1]["kind"] != "secret":
                return "只有上报 Token 可以用 clear 清空", {}, []

        new_doc = copy.deepcopy(self.user_doc)
        restart: List[str] = []
        touched: List[str] = []
        for key in delete_keys:
            field = FIELDS[key][1]
            _delete_path(new_doc, key)
            for alias in _alias_keys(key):
                _delete_path(new_doc, alias)
            touched.append(key)
            if field.get("restart"):
                restart.append(key)
        for key, raw in values.items():
            field = FIELDS[key][1]
            if field["kind"] == "secret" and key in clear:
                return "不能同时填写和清空%s" % field["label"], {}, []
            error, value = _coerce_field(field, raw)
            if error:
                return error, {}, []
            if value is None:
                _delete_path(new_doc, key)
            else:
                _set_path(new_doc, key, value)
            touched.append(key)
            if field.get("restart"):
                old_present, old_value = _lookup(self.effective, key)
                if (not old_present) or old_value != value:
                    restart.append(key)
        try:
            effective = merge_center_config(new_doc)
        except ValueError as exc:
            return str(exc), {}, []
        relation = _relation_error(effective, touched)
        if relation:
            return relation, {}, []
        # 重启项：和当前生效值相比没有变化的不要报。
        changed_restart = []
        for key in restart:
            _old_ok, old_value = _lookup(self.effective, key)
            _new_ok, new_value = _lookup(effective, key)
            if old_value != new_value and key not in changed_restart:
                changed_restart.append(key)
        return None, new_doc, changed_restart

    def _push_live(self) -> None:
        cfg = self.effective
        if self.storage is not None:
            self.storage.retention_days = _cfg_int(cfg.get("retention_days"), 7)
            self.storage.offline_seconds = _cfg_int(cfg.get("offline_seconds"), 90)
        if self.cleanup is not None:
            self.cleanup.interval = _cfg_int(cfg.get("cleanup_interval_seconds"), 3600)
        if self.scheduler is not None:
            busy = period_util_thresholds(cfg.get("period_util") or {})
            anomaly = dict(cfg.get("anomaly") or {})
            report = dict(cfg.get("report") or {})
            schedule = cfg.get("report_schedule")
            updater = getattr(self.scheduler, "update_inputs", None)
            if updater is not None:
                updater(schedule, busy, anomaly, report)
            else:
                self.scheduler.schedule = normalize_report_schedule(schedule)
                self.scheduler.busy_thresholds = busy
                self.scheduler.anomaly_thresholds = anomaly
                self.scheduler.report_rules = report
        if self.deploy_runner is not None:
            self.deploy_runner.token = str(cfg.get("token") or "")
            if self.project_root:
                from center.python_bundle import bundles_dir_from_config

                self.deploy_runner.bundles_dir = bundles_dir_from_config(
                    cfg, self.project_root
                )


def _cfg_int(value: Any, default: int) -> int:
    if value is None or value == "" or isinstance(value, bool):
        return int(default)
    return int(value)

