"""资源异常判断（仅判定与展示，不含通知/修复动作）。

默认阈值：偏高(warn)=黄，异常(critical)=红。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from center.storage import Storage


DEFAULT_THRESHOLDS = {
    "cpu_warn_percent": 80.0,
    "cpu_critical_percent": 95.0,
    "cpu_load_per_core_warn": 2.0,
    "mem_warn_percent": 85.0,
    "mem_critical_percent": 95.0,
    "disk_warn_percent": 85.0,
    "disk_critical_percent": 95.0,
    # 加速卡（NVIDIA / 华为 / 寒武纪 / 瑞芯微等）共用
    "accel_util_warn_percent": 95.0,
    "accel_temp_warn_c": 85.0,
    "accel_temp_critical_c": 95.0,
    "accel_mem_warn_percent": 90.0,
    "accel_mem_critical_percent": 98.0,
    # 兼容旧配置键名
    "npu_util_warn_percent": 95.0,
    "npu_temp_warn_c": 85.0,
    "npu_temp_critical_c": 95.0,
    "npu_mem_warn_percent": 90.0,
    "npu_mem_critical_percent": 98.0,
}


def _num(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _level(score: int) -> str:
    if score >= 2:
        return "critical"
    if score >= 1:
        return "warn"
    return "normal"


def _merged_thresholds(thresholds: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    th = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        th.update({k: v for k, v in thresholds.items() if v is not None})
    # 旧键映射到新键（若新键未单独覆盖）
    aliases = {
        "accel_util_warn_percent": "npu_util_warn_percent",
        "accel_temp_warn_c": "npu_temp_warn_c",
        "accel_temp_critical_c": "npu_temp_critical_c",
        "accel_mem_warn_percent": "npu_mem_warn_percent",
        "accel_mem_critical_percent": "npu_mem_critical_percent",
    }
    for new_k, old_k in aliases.items():
        if thresholds and new_k not in thresholds and old_k in th:
            th[new_k] = th[old_k]
    return th


def _collect_cards(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    cards = list(payload.get("accelerators") or [])
    if cards:
        return cards
    cards = []
    for n in payload.get("npus") or []:
        item = dict(n)
        item.setdefault("vendor", "huawei")
        item.setdefault("vendor_label", "加速卡")
        cards.append(item)
    for g in payload.get("gpus") or []:
        item = dict(g)
        item.setdefault("vendor", "nvidia")
        item.setdefault("vendor_label", "英伟达")
        cards.append(item)
    return cards


def judge_host_payload(
    payload: Dict[str, Any],
    thresholds: Optional[Dict[str, Any]] = None,
    online: bool = True,
) -> Dict[str, Any]:
    th = _merged_thresholds(thresholds)
    system = payload.get("system") or {}
    cards = _collect_cards(payload)

    findings: List[Dict[str, Any]] = []
    cpu_score = 0
    sys_score = 0
    accel_score = 0

    if not online:
        return {
            "overall": "unknown",
            "cpu": {"status": "unknown", "findings": []},
            "system": {"status": "unknown", "findings": []},
            "accel": {"status": "unknown", "findings": [], "cards": []},
            "npu": {"status": "unknown", "findings": [], "cards": []},
            "findings": [
                {
                    "resource": "host",
                    "level": "unknown",
                    "code": "offline",
                    "message": "主机离线，无法判定资源状态",
                }
            ],
        }

    cpu = _num(system.get("cpu_percent"))
    load1 = _num(system.get("load1"))
    cores = _num(system.get("cpu_count")) or 1

    if cpu is not None:
        crit = _num(th["cpu_critical_percent"]) or 95
        warn = _num(th["cpu_warn_percent"]) or 80
        if cpu >= crit:
            cpu_score = max(cpu_score, 2)
            findings.append(
                {
                    "resource": "cpu",
                    "level": "critical",
                    "code": "cpu_util_critical",
                    "message": "CPU 利用率异常高：%.1f%%（阈值 ≥%.0f%%）" % (cpu, crit),
                    "value": cpu,
                }
            )
        elif cpu >= warn:
            cpu_score = max(cpu_score, 1)
            findings.append(
                {
                    "resource": "cpu",
                    "level": "warn",
                    "code": "cpu_util_warn",
                    "message": "CPU 利用率偏高：%.1f%%（阈值 ≥%.0f%%）" % (cpu, warn),
                    "value": cpu,
                }
            )

    if load1 is not None and cores > 0:
        ratio = load1 / cores
        limit = _num(th["cpu_load_per_core_warn"]) or 2.0
        if ratio >= limit:
            cpu_score = max(cpu_score, 1)
            findings.append(
                {
                    "resource": "cpu",
                    "level": "warn",
                    "code": "cpu_load_high",
                    "message": "CPU 负载偏高：load1=%.2f / %d核 = %.2f" % (load1, int(cores), ratio),
                    "value": round(ratio, 2),
                }
            )

    mem = _num(system.get("mem_percent"))
    if mem is not None:
        m_crit = _num(th["mem_critical_percent"]) or 95
        m_warn = _num(th["mem_warn_percent"]) or 85
        if mem >= m_crit:
            sys_score = max(sys_score, 2)
            findings.append(
                {
                    "resource": "mem",
                    "level": "critical",
                    "code": "mem_critical",
                    "message": "内存使用异常高：%.1f%%（阈值 ≥%.0f%%）" % (mem, m_crit),
                    "value": mem,
                }
            )
        elif mem >= m_warn:
            sys_score = max(sys_score, 1)
            findings.append(
                {
                    "resource": "mem",
                    "level": "warn",
                    "code": "mem_warn",
                    "message": "内存使用偏高：%.1f%%（阈值 ≥%.0f%%）" % (mem, m_warn),
                    "value": mem,
                }
            )

    disk = _num(system.get("disk_percent"))
    if disk is not None:
        d_crit = _num(th["disk_critical_percent"]) or 95
        d_warn = _num(th["disk_warn_percent"]) or 85
        if disk >= d_crit:
            sys_score = max(sys_score, 2)
            findings.append(
                {
                    "resource": "disk",
                    "level": "critical",
                    "code": "disk_critical",
                    "message": "磁盘使用异常高：%.1f%%（阈值 ≥%.0f%%）" % (disk, d_crit),
                    "value": disk,
                }
            )
        elif disk >= d_warn:
            sys_score = max(sys_score, 1)
            findings.append(
                {
                    "resource": "disk",
                    "level": "warn",
                    "code": "disk_warn",
                    "message": "磁盘使用偏高：%.1f%%（阈值 ≥%.0f%%）" % (disk, d_warn),
                    "value": disk,
                }
            )

    card_results: List[Dict[str, Any]] = []
    util_warn = _num(th.get("accel_util_warn_percent")) or _num(th.get("npu_util_warn_percent")) or 95
    t_crit = _num(th.get("accel_temp_critical_c")) or _num(th.get("npu_temp_critical_c")) or 95
    t_warn = _num(th.get("accel_temp_warn_c")) or _num(th.get("npu_temp_warn_c")) or 85
    m_crit = _num(th.get("accel_mem_critical_percent")) or _num(th.get("npu_mem_critical_percent")) or 98
    m_warn = _num(th.get("accel_mem_warn_percent")) or _num(th.get("npu_mem_warn_percent")) or 90

    for n in cards:
        idx = n.get("index", "?")
        vendor = n.get("vendor_label") or n.get("vendor") or "加速卡"
        card_findings: List[Dict[str, Any]] = []
        score = 0
        health = (n.get("health") or "").upper()
        if health and health not in ("OK", "", "UNKNOWN"):
            if health in ("CRITICAL", "ALARM"):
                score = max(score, 2)
                level = "critical"
            else:
                score = max(score, 1)
                level = "warn"
            card_findings.append(
                {
                    "resource": "accel",
                    "level": level,
                    "code": "accel_health",
                    "message": "%s#%s Health=%s" % (vendor, idx, n.get("health")),
                    "value": n.get("health"),
                }
            )

        util = _num(n.get("util_percent"))
        if util is not None and util >= util_warn:
            score = max(score, 1)
            card_findings.append(
                {
                    "resource": "accel",
                    "level": "warn",
                    "code": "accel_util_high",
                    "message": "%s#%s 利用率高：%.1f%%（阈值 ≥%.0f%%）" % (vendor, idx, util, util_warn),
                    "value": util,
                }
            )

        temp = _num(n.get("temp_c"))
        if temp is not None:
            if temp >= t_crit:
                score = max(score, 2)
                card_findings.append(
                    {
                        "resource": "accel",
                        "level": "critical",
                        "code": "accel_temp_critical",
                        "message": "%s#%s 温度异常：%.0f°C（阈值 ≥%.0f°C）" % (vendor, idx, temp, t_crit),
                        "value": temp,
                    }
                )
            elif temp >= t_warn:
                score = max(score, 1)
                card_findings.append(
                    {
                        "resource": "accel",
                        "level": "warn",
                        "code": "accel_temp_warn",
                        "message": "%s#%s 温度偏高：%.0f°C（阈值 ≥%.0f°C）" % (vendor, idx, temp, t_warn),
                        "value": temp,
                    }
                )

        mem_pct = _num(n.get("mem_percent"))
        if mem_pct is None:
            mu = _num(n.get("mem_used_mb"))
            mt = _num(n.get("mem_total_mb"))
            if mu is not None and mt and mt > 0:
                mem_pct = mu * 100.0 / mt
        if mem_pct is not None:
            if mem_pct >= m_crit:
                score = max(score, 2)
                card_findings.append(
                    {
                        "resource": "accel",
                        "level": "critical",
                        "code": "accel_mem_critical",
                        "message": "%s#%s 显存/HBM 异常高：%.1f%%" % (vendor, idx, mem_pct),
                        "value": round(mem_pct, 2),
                    }
                )
            elif mem_pct >= m_warn:
                score = max(score, 1)
                card_findings.append(
                    {
                        "resource": "accel",
                        "level": "warn",
                        "code": "accel_mem_warn",
                        "message": "%s#%s 显存/HBM 偏高：%.1f%%" % (vendor, idx, mem_pct),
                        "value": round(mem_pct, 2),
                    }
                )

        accel_score = max(accel_score, score)
        findings.extend(card_findings)
        card_results.append(
            {
                "index": idx,
                "name": n.get("name"),
                "vendor": n.get("vendor"),
                "vendor_label": vendor,
                "status": _level(score),
                "findings": card_findings,
            }
        )

    overall_score = max(cpu_score, sys_score, accel_score)
    accel_block = {
        "status": _level(accel_score) if cards else "normal",
        "card_count": len(cards),
        "cards": card_results,
        "findings": [f for f in findings if f["resource"] == "accel"],
    }
    return {
        "overall": _level(overall_score),
        "cpu": {"status": _level(cpu_score), "findings": [f for f in findings if f["resource"] == "cpu"]},
        "system": {
            "status": _level(sys_score),
            "findings": [f for f in findings if f["resource"] in ("mem", "disk")],
        },
        "accel": accel_block,
        # 兼容旧前端字段名
        "npu": accel_block,
        "findings": findings,
        "thresholds": th,
    }


def evaluate_cluster(
    storage: Storage,
    thresholds: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    hosts_out: List[Dict[str, Any]] = []
    summary = {"normal": 0, "warn": 0, "critical": 0, "unknown": 0}
    th = _merged_thresholds(thresholds)

    for h in storage.list_hosts():
        detail = storage.get_host(h["host_id"])
        payload = (detail or {}).get("payload") or {}
        judged = judge_host_payload(payload, thresholds=thresholds, online=bool(h.get("online")))
        status = judged["overall"]
        summary[status] = summary.get(status, 0) + 1
        hosts_out.append(
            {
                "host_id": h["host_id"],
                "hostname": h.get("hostname"),
                "host_type": h.get("host_type"),
                "online": h.get("online"),
                "cpu_percent": h.get("cpu_percent"),
                "npu_count": h.get("npu_count"),
                "npu_util_avg": h.get("npu_util_avg"),
                "gpu_count": h.get("gpu_count"),
                "anomaly": judged,
            }
        )

    return {
        "summary": summary,
        "hosts": hosts_out,
        "thresholds": th,
    }
