"""采集端本地队列：上报失败先落盘，恢复后再按原采集时间补报。

队列在安装目录的 data/spool（生产默认 /opt/monitor-agent/data/spool）。
进程重启和覆盖安装都保留 data/，所以断网期间的样本还在。
超过保留时间或条数上限时丢掉最旧的，并记下溢出，稍后告诉中心。
"""

import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_RETENTION_HOURS = 24
DEFAULT_BATCH_SIZE = 80
MAX_BATCH_SIZE = 100
# 采样间隔最短 5 秒。条数上限按保留时长除以这个间隔，避免磁盘被写满。
MIN_INTERVAL_SECONDS = 5
_ADJACENT_SECONDS = 120


def resolve_spool_dir(cfg: Dict[str, Any], config_path: Optional[str]) -> str:
    explicit = cfg.get("spool_dir")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    root = os.getcwd()
    if config_path:
        parent = os.path.dirname(os.path.abspath(config_path))
        if os.path.basename(parent) == "config":
            root = os.path.dirname(parent)
        else:
            root = parent
    return os.path.join(root, "data", "spool")


def batch_url_for(center_url: str) -> str:
    base = (center_url or "").strip().rstrip("/")
    if base.endswith("/api/v1/metrics"):
        return base + "/batch"
    return base + "/batch"


def clamp_batch_size(value: Any) -> int:
    try:
        size = int(value)
    except (TypeError, ValueError):
        size = DEFAULT_BATCH_SIZE
    if size < 1:
        size = 1
    if size > MAX_BATCH_SIZE:
        size = MAX_BATCH_SIZE
    return size


def parse_retention_hours(value: Any, default: int = DEFAULT_RETENTION_HOURS) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return int(default)
    if hours < 1:
        return int(default)
    if hours > 8760:
        return 8760
    return hours


def retention_hours_from_body(body: str) -> Optional[int]:
    if not body:
        return None
    try:
        data = json.loads(body)
    except ValueError:
        return None
    if not isinstance(data, dict) or "spool_retention_hours" not in data:
        return None
    try:
        hours = int(data.get("spool_retention_hours"))
    except (TypeError, ValueError):
        return None
    if hours < 1 or hours > 8760:
        return None
    return hours


class Spool:
    def __init__(
        self,
        directory: str,
        retention_hours: int = DEFAULT_RETENTION_HOURS,
        max_samples: Optional[int] = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        self.directory = directory
        self.retention_hours = parse_retention_hours(retention_hours)
        self._max_samples = None
        if max_samples is not None and max_samples != "":
            try:
                cap = int(max_samples)
            except (TypeError, ValueError):
                cap = 0
            if cap > 0:
                self._max_samples = cap
        self.batch_size = clamp_batch_size(batch_size)
        os.makedirs(self.directory, exist_ok=True)
        hinted = self._read_hint()
        if hinted is not None:
            self.retention_hours = hinted
        self._write_items(self._evict(self._load_items()))

    def _items_path(self) -> str:
        return os.path.join(self.directory, "items.jsonl")

    def _overflow_path(self) -> str:
        return os.path.join(self.directory, "overflows.json")

    def _hint_path(self) -> str:
        return os.path.join(self.directory, "retention.json")

    def max_samples(self) -> int:
        if self._max_samples is not None:
            return int(self._max_samples)
        return max(1, int(self.retention_hours) * 3600 // MIN_INTERVAL_SECONDS)

    def set_retention_hours(self, hours: int) -> None:
        parsed = parse_retention_hours(hours, default=self.retention_hours)
        self.retention_hours = parsed
        self._write_hint(parsed)
        self._write_items(self._evict(self._load_items()))

    def enqueue(self, payload: Dict[str, Any], now: Optional[int] = None) -> str:
        """失败样本入队。返回 queued，或因保留/条数上限被丢掉时返回 overflow。"""
        item = _item_from_payload(payload, now=now)
        if item is None:
            return "overflow"
        items = self._load_items()
        replaced = False
        for index, old in enumerate(items):
            if int(old["timestamp"]) == int(item["timestamp"]):
                items[index] = item
                replaced = True
                break
        if not replaced:
            items.append(item)
        kept = self._evict(items, now=now)
        self._write_items(kept)
        kept_ts = {int(row["timestamp"]) for row in kept}
        if int(item["timestamp"]) in kept_ts:
            return "queued"
        return "overflow"

    def peek(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        size = self.batch_size if limit is None else clamp_batch_size(limit)
        rows = self._load_items()
        out: List[Dict[str, Any]] = []
        for row in rows[:size]:
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            out.append(
                {
                    "timestamp": int(row["timestamp"]),
                    "payload": json.loads(json.dumps(payload)),
                }
            )
        return out

    def pending_overflows(self) -> List[Dict[str, Any]]:
        return self._load_overflows()

    def ack(self, timestamps: List[Any]) -> None:
        drop = set()
        for raw in timestamps or []:
            try:
                drop.add(int(raw))
            except (TypeError, ValueError):
                continue
        if not drop:
            return
        kept = [row for row in self._load_items() if int(row["timestamp"]) not in drop]
        self._write_items(kept)

    def ack_overflows(self) -> None:
        self._write_overflows([])

    def _evict(self, items: List[Dict[str, Any]], now: Optional[int] = None) -> List[Dict[str, Any]]:
        clock = int(now if now is not None else time.time())
        cutoff = clock - int(self.retention_hours) * 3600
        kept: List[Dict[str, Any]] = []
        aged: List[Dict[str, Any]] = []
        for row in items:
            try:
                ts = int(row["timestamp"])
            except (TypeError, ValueError, KeyError):
                continue
            if ts < cutoff:
                aged.append(row)
            else:
                kept.append(row)
        self._note_dropped(aged, "retention")
        kept.sort(key=lambda row: int(row["timestamp"]))
        cap = self.max_samples()
        if len(kept) > cap:
            extra = kept[: len(kept) - cap]
            kept = kept[len(kept) - cap :]
            self._note_dropped(extra, "capacity")
        return kept

    def _note_dropped(self, rows: List[Dict[str, Any]], reason: str) -> None:
        if not rows:
            return
        stamps = [int(row["timestamp"]) for row in rows]
        host_id = ""
        for row in rows:
            payload = row.get("payload") or {}
            if isinstance(payload, dict) and payload.get("host_id"):
                host_id = str(payload.get("host_id"))
        event = {
            "host_id": host_id,
            "from_ts": min(stamps),
            "to_ts": max(stamps),
            "dropped": len(rows),
            "reason": reason,
        }
        events = self._load_overflows()
        merged = False
        for old in events:
            if str(old.get("reason") or "") != reason:
                continue
            if str(old.get("host_id") or "") != host_id:
                continue
            old_from = int(old.get("from_ts") or 0)
            old_to = int(old.get("to_ts") or 0)
            if event["from_ts"] <= old_to + _ADJACENT_SECONDS and old_from <= event["to_ts"] + _ADJACENT_SECONDS:
                old["from_ts"] = min(old_from, event["from_ts"])
                old["to_ts"] = max(old_to, event["to_ts"])
                old["dropped"] = int(old.get("dropped") or 0) + event["dropped"]
                merged = True
                break
        if not merged:
            events.append(event)
        self._write_overflows(events)

    def _load_items(self) -> List[Dict[str, Any]]:
        path = self._items_path()
        if not os.path.isfile(path):
            return []
        rows: List[Dict[str, Any]] = []
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            return []
        try:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    row = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(row, dict) or not isinstance(row.get("payload"), dict):
                    continue
                try:
                    row["timestamp"] = int(row.get("timestamp") or row["payload"].get("timestamp"))
                except (TypeError, ValueError):
                    continue
                rows.append(row)
        finally:
            handle.close()
        rows.sort(key=lambda row: int(row["timestamp"]))
        return rows

    def _write_items(self, rows: List[Dict[str, Any]]) -> None:
        lines = []
        ordered = sorted(rows, key=lambda row: int(row["timestamp"]))
        for row in ordered:
            lines.append(json.dumps(row, ensure_ascii=False, sort_keys=True))
        _atomic_write(self._items_path(), "\n".join(lines) + ("\n" if lines else ""))

    def _load_overflows(self) -> List[Dict[str, Any]]:
        path = self._overflow_path()
        if not os.path.isfile(path):
            return []
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            return []
        try:
            raw = handle.read()
        finally:
            handle.close()
        try:
            data = json.loads(raw) if raw.strip() else []
        except ValueError:
            return []
        if not isinstance(data, list):
            return []
        events = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                events.append(
                    {
                        "host_id": str(item.get("host_id") or ""),
                        "from_ts": int(item.get("from_ts")),
                        "to_ts": int(item.get("to_ts")),
                        "dropped": int(item.get("dropped") or 0),
                        "reason": str(item.get("reason") or ""),
                    }
                )
            except (TypeError, ValueError):
                continue
        return events

    def _write_overflows(self, events: List[Dict[str, Any]]) -> None:
        _atomic_write(
            self._overflow_path(),
            json.dumps(events, ensure_ascii=False, sort_keys=True),
        )

    def _read_hint(self) -> Optional[int]:
        path = self._hint_path()
        if not os.path.isfile(path):
            return None
        try:
            handle = open(path, "r", encoding="utf-8")
        except OSError:
            return None
        try:
            raw = handle.read()
        finally:
            handle.close()
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        return parse_retention_hours(data.get("hours"), default=self.retention_hours)

    def _write_hint(self, hours: int) -> None:
        _atomic_write(
            self._hint_path(),
            json.dumps({"hours": int(hours)}, ensure_ascii=False),
        )


def _item_from_payload(payload: Dict[str, Any], now: Optional[int] = None) -> Optional[Dict[str, Any]]:
    if not isinstance(payload, dict):
        return None
    try:
        ts = int(payload.get("timestamp"))
    except (TypeError, ValueError):
        return None
    cloned = json.loads(json.dumps(payload))
    cloned["timestamp"] = ts
    cloned.pop("backfill", None)
    return {
        "timestamp": ts,
        "enqueued_at": int(now if now is not None else time.time()),
        "payload": cloned,
    }


def _atomic_write(path: str, text: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    handle = open(tmp, "w", encoding="utf-8")
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(tmp, path)


def build_flush_body(
    spool: Spool,
    token: str = "",
    host_id: str = "",
    limit: Optional[int] = None,
) -> Optional[Tuple[Dict[str, Any], List[int]]]:
    """拼一批补报。没有待发样本、也没有溢出时返回 None。"""
    peeked = spool.peek(limit)
    overflows = spool.pending_overflows()
    if not peeked and not overflows:
        return None
    samples = []
    stamps: List[int] = []
    for item in peeked:
        sample = dict(item["payload"])
        sample["backfill"] = True
        sample["timestamp"] = int(item["timestamp"])
        if token and not sample.get("token"):
            sample["token"] = token
        samples.append(sample)
        stamps.append(int(item["timestamp"]))
    body: Dict[str, Any] = {"samples": samples, "overflows": overflows}
    if token:
        body["token"] = token
    hid = host_id
    if not hid and samples:
        hid = str(samples[0].get("host_id") or "")
    if not hid and overflows:
        hid = str(overflows[0].get("host_id") or "")
    if hid:
        body["host_id"] = hid
    return body, stamps


def apply_flush_response(spool: Spool, stamps: List[int], code: int, body: str) -> None:
    """中心确认后才从队列删掉。失败留下次再送，避免补报丢在半路。"""
    if not (200 <= int(code) < 300):
        return
    try:
        data = json.loads(body) if body else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    ack_ts = list(stamps)
    rejected = data.get("rejected") or []
    if isinstance(rejected, list):
        for item in rejected:
            if isinstance(item, dict) and item.get("timestamp") is not None:
                try:
                    ack_ts.append(int(item["timestamp"]))
                except (TypeError, ValueError):
                    continue
    # 先确认中心已经收下，再按新的保留时间丢掉还没送出的旧样本。
    spool.ack(ack_ts)
    if data.get("ok"):
        spool.ack_overflows()
    hours = retention_hours_from_body(body)
    if hours is not None:
        spool.set_retention_hours(hours)
