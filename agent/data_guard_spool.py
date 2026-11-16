"""重点数据事件的短时落盘。

v1.12 的断网补报是 ``agent.spool.enqueue_data_guard(spool, payload)``，
整笔指标（含 data_guard）进 ``data/spool``。采集主循环在发送失败时走那条队列，
不再把同一笔写进这里。

本文件只留给「没有经过指标队列」的调用：测试，或发送前就失败、payload 还没入队。
它不是第二套 24 小时指标库。若另有 ``agent.report_spool.enqueue_data_guard(records)``，
失败时仍会试着交给它。
"""

import json
import os
from typing import Any, Dict, List, Optional


SPOOL_NAME = "events.json"
STATE_NAME = "state.json"
# 只覆盖短时发送失败，不做成 24 小时指标库。
MAX_RECORDS = 50
MAX_AGE_SECONDS = 2 * 3600


def spool_path(state_dir):
    # type: (str) -> str
    return os.path.join(state_dir, SPOOL_NAME)


def state_path(state_dir):
    # type: (str) -> str
    return os.path.join(state_dir, STATE_NAME)


def _write_json(path, obj):
    # type: (str, Any) -> None
    directory = os.path.dirname(path)
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_json(path, fallback):
    # type: (str, Any) -> Any
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


def sibling_enqueue(records):
    # type: (List[Dict[str, Any]]) -> bool
    """交给 v1.12 的 report_spool。模块或函数不在时返回 False，不报错。"""
    try:
        import importlib

        module = importlib.import_module("agent.report_spool")
    except ImportError:
        return False
    enqueue = getattr(module, "enqueue_data_guard", None)
    if not callable(enqueue):
        return False
    try:
        enqueue(records)
    except Exception:
        return False
    return True


def load_records(state_dir, now):
    # type: (str, int) -> List[Dict[str, Any]]
    raw = _read_json(spool_path(state_dir), [])
    if not isinstance(raw, list):
        return []
    kept = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        ts = item.get("ts")
        try:
            ts_int = int(ts)
        except (TypeError, ValueError):
            continue
        if now - ts_int > MAX_AGE_SECONDS or ts_int - now > 300:
            continue
        kept.append(item)
    if len(kept) > MAX_RECORDS:
        kept = kept[-MAX_RECORDS:]
    return kept


def persist_records(state_dir, records, now):
    # type: (str, List[Dict[str, Any]], int) -> None
    """发送失败时调用。本地文件必写；补报模块在的话再交一份。"""
    existing = load_records(state_dir, now)
    existing.extend(records)
    if len(existing) > MAX_RECORDS:
        existing = existing[-MAX_RECORDS:]
    _write_json(spool_path(state_dir), existing)
    sibling_enqueue(records)


def clear_records(state_dir):
    # type: (str) -> None
    path = spool_path(state_dir)
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:
        _write_json(path, [])


def load_state(state_dir):
    # type: (str) -> Dict[str, Any]
    raw = _read_json(state_path(state_dir), {})
    return raw if isinstance(raw, dict) else {}


def save_state(state_dir, state):
    # type: (str, Dict[str, Any]) -> None
    _write_json(state_path(state_dir), state)
