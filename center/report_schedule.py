"""中心进程内的诊断报告调度。

只在配置的那一分钟触发，不为停机错过的时刻补生成。
同一 (period, 窗口) 已有报告则跳过，不覆盖手动结果。
"""

from __future__ import annotations

import queue
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from center.reports import SHANGHAI, handle_generate_report, report_period_bounds


DEFAULT_REPORT_SCHEDULE: Dict[str, Any] = {
    "enabled": True,
    "daily": {"enabled": True, "hour": 0, "minute": 10},
    "weekly": {"enabled": True, "hour": 0, "minute": 20},
    "monthly": {"enabled": True, "hour": 0, "minute": 30},
}


def deliver_report(report: Dict[str, Any]) -> None:
    """下一阶段推送挂钩（企业微信 / 钉钉 / 飞书）。

    调度器只在成功写入新报告后调用。这里不发任何外部请求。
    """
    return None


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
    return default


def _parse_clock(raw: Any, default_hour: int, default_minute: int) -> Optional[Dict[str, Any]]:
    """合法返回槽位；时刻非法返回 None（该周期不触发）。enabled 缺省为 True。"""
    slot = raw if isinstance(raw, dict) else {}
    try:
        hour = int(slot["hour"]) if "hour" in slot and slot["hour"] != "" else default_hour
        minute = (
            int(slot["minute"])
            if "minute" in slot and slot["minute"] != ""
            else default_minute
        )
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return {
        "enabled": _as_bool(slot.get("enabled", True), True),
        "hour": hour,
        "minute": minute,
    }


def merge_report_schedule(raw: Any) -> Any:
    """把用户配置叠到默认值上。缺省键保留默认；非法时刻原样留下交给校验。"""
    base: Dict[str, Any] = {
        "enabled": True,
        "daily": dict(DEFAULT_REPORT_SCHEDULE["daily"]),
        "weekly": dict(DEFAULT_REPORT_SCHEDULE["weekly"]),
        "monthly": dict(DEFAULT_REPORT_SCHEDULE["monthly"]),
    }
    if raw is None:
        return base
    if not isinstance(raw, dict):
        return raw
    if "enabled" in raw:
        base["enabled"] = raw.get("enabled")
    for key in ("daily", "weekly", "monthly"):
        slot = raw.get(key)
        if slot is None:
            continue
        if not isinstance(slot, dict):
            base[key] = slot
            continue
        merged = dict(base[key])
        merged.update(slot)
        base[key] = merged
    return base


def normalize_report_schedule(raw: Any) -> Dict[str, Any]:
    """缺省键用默认时刻。总开关缺省为开。非法时刻的周期视为关闭。"""
    user = raw if isinstance(raw, dict) else {}
    schedule: Dict[str, Any] = {
        "enabled": _as_bool(user.get("enabled", True), True),
    }
    defaults = (
        ("daily", 0, 10),
        ("weekly", 0, 20),
        ("monthly", 0, 30),
    )
    for key, hour, minute in defaults:
        parsed = _parse_clock(user.get(key), hour, minute)
        if parsed is None:
            schedule[key] = {"enabled": False, "hour": hour, "minute": minute}
        else:
            schedule[key] = parsed
    return schedule


def due_slots(schedule: Optional[Dict[str, Any]], now_ts: int) -> List[Dict[str, Any]]:
    """当前这一分钟到期的槽位。不回看更早的分钟，因此错过即跳过。"""
    spec = normalize_report_schedule(schedule)
    if not spec.get("enabled"):
        return []
    local = datetime.fromtimestamp(int(now_ts), SHANGHAI)
    catalog = (
        ("day", "daily", True),
        ("week", "weekly", local.weekday() == 0),
        ("month", "monthly", local.day == 1),
    )
    found: List[Dict[str, Any]] = []
    for period, key, day_ok in catalog:
        slot = spec[key]
        if not day_ok or not slot.get("enabled"):
            continue
        if local.hour != int(slot["hour"]) or local.minute != int(slot["minute"]):
            continue
        bounds = report_period_bounds(period, int(now_ts))
        found.append(
            {
                "key": "%s:%s" % (period, local.strftime("%Y-%m-%d")),
                "period": period,
                "as_of": int(now_ts),
                "from_ts": bounds["from_ts"],
                "to_ts": bounds["to_ts"],
            }
        )
    return found


def describe_schedule(schedule: Optional[Dict[str, Any]]) -> str:
    spec = normalize_report_schedule(schedule)
    if not spec.get("enabled"):
        return "未启用"
    parts = []
    labels = (
        ("daily", "日报"),
        ("weekly", "周报 周一"),
        ("monthly", "月报 每月1日"),
    )
    for key, label in labels:
        slot = spec[key]
        if not slot.get("enabled"):
            parts.append("%s 关" % label)
            continue
        parts.append("%s %02d:%02d" % (label, slot["hour"], slot["minute"]))
    return "、".join(parts) + "（Asia/Shanghai，错过不补）"


class ReportScheduler:
    """检查线程与生成线程分开，避免一份月报算太久而错过下一分钟。"""

    def __init__(
        self,
        storage: Any,
        schedule: Optional[Dict[str, Any]] = None,
        busy_thresholds: Optional[Dict[str, float]] = None,
        anomaly_thresholds: Optional[Dict[str, Any]] = None,
        poll_seconds: int = 20,
        clock: Optional[Callable[[], int]] = None,
    ) -> None:
        self.storage = storage
        self.schedule = normalize_report_schedule(schedule)
        self.busy_thresholds = busy_thresholds
        self.anomaly_thresholds = anomaly_thresholds
        self.poll_seconds = max(5, int(poll_seconds or 20))
        self._clock = clock
        self._fired = set()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []

    def _now(self) -> int:
        if self._clock is None:
            return int(time.time())
        return int(self._clock())

    def enqueue_due(self, now_ts: Optional[int] = None) -> List[str]:
        """把当前分钟内尚未尝试的槽位放进队列。返回新入队的 key。"""
        moment = int(now_ts if now_ts is not None else self._now())
        keys: List[str] = []
        with self._lock:
            for slot in due_slots(self.schedule, moment):
                key = slot["key"]
                if key in self._fired:
                    continue
                self._fired.add(key)
                self._queue.put(slot)
                keys.append(key)
        return keys

    def run_due(self, now_ts: Optional[int] = None) -> List[Dict[str, Any]]:
        """同步执行当前分钟到期的槽位。测试用，不经过队列。"""
        moment = int(now_ts if now_ts is not None else self._now())
        results: List[Dict[str, Any]] = []
        pending: List[Dict[str, Any]] = []
        with self._lock:
            for slot in due_slots(self.schedule, moment):
                if slot["key"] in self._fired:
                    continue
                self._fired.add(slot["key"])
                pending.append(slot)
        for slot in pending:
            results.append(self._generate_job(slot))
        return results

    def _generate_job(self, slot: Dict[str, Any]) -> Dict[str, Any]:
        clock_ts = self._now()
        try:
            code, payload = handle_generate_report(
                self.storage,
                body=b"",
                query="period=%s&as_of=%s" % (slot["period"], int(slot["as_of"])),
                busy_thresholds=self.busy_thresholds,
                anomaly_thresholds=self.anomaly_thresholds,
                source="scheduled",
                replace=False,
                generated_at=clock_ts,
                clock=clock_ts,
            )
        except Exception as exc:  # noqa: BLE001
            print(
                "[report-schedule] %s 生成异常: %s" % (slot.get("key"), exc),
                flush=True,
            )
            return {"ok": False, "error": str(exc), "slot": slot}
        report = (payload or {}).get("report") or {}
        if payload.get("skipped"):
            print(
                "[report-schedule] %s 已有报告 id=%s，跳过"
                % (slot.get("key"), report.get("id")),
                flush=True,
            )
        elif code == 200 and report.get("status", "ok") == "ok":
            print(
                "[report-schedule] %s 已生成 id=%s"
                % (slot.get("key"), report.get("id")),
                flush=True,
            )
            try:
                deliver_report(report)
            except Exception as exc:  # noqa: BLE001
                print("[report-schedule] 推送挂钩失败: %s" % exc, flush=True)
        else:
            print(
                "[report-schedule] %s 失败: %s"
                % (slot.get("key"), (payload or {}).get("error")),
                flush=True,
            )
        return payload

    def start(self) -> None:
        if self._threads:
            return
        checker = threading.Thread(
            target=self._loop, name="report-schedule", daemon=True
        )
        worker = threading.Thread(
            target=self._worker, name="report-schedule-worker", daemon=True
        )
        self._threads = [checker, worker]
        checker.start()
        worker.start()
        print("[report-schedule] %s" % describe_schedule(self.schedule), flush=True)

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.enqueue_due(self._now())
            except Exception as exc:  # noqa: BLE001
                print("[report-schedule] 检查失败: %s" % exc, flush=True)
            self._stop.wait(self.poll_seconds)

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                slot = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._generate_job(slot)
            except Exception as exc:  # noqa: BLE001
                print("[report-schedule] 生成失败: %s" % exc, flush=True)
