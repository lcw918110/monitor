"""采集端本地暂存。

上报在短重试后仍失败时，把样本按原来的采集时间写入 data/spool。
进程重启后队列还在。发送成功后按小批量补报，并标成 backfill。
超过保留时间的最早样本会被丢掉，并留下一条溢出记录，恢复后交给中心。
"""

import json
import os
import time


DEFAULT_RETENTION_HOURS = 24
DEFAULT_BATCH_SIZE = 80
MIN_RETENTION_HOURS = 1
MAX_RETENTION_HOURS = 168
MAX_BATCH_SIZE = 100


def coerce_retention_hours(value):
    """合法小时数，否则 None。拒绝布尔值。"""
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if isinstance(value, float) and abs(value - number) > 1e-9:
        return None
    if number < MIN_RETENTION_HOURS or number > MAX_RETENTION_HOURS:
        return None
    return number


def resolve_spool_dir(cfg):
    """暂存目录。显式 spool_dir 优先，其次 data_dir/spool，否则相对 data/spool。

    生产安装的工作目录是 /opt/monitor-agent，因此默认落在
    /opt/monitor-agent/data/spool。升级同步会排除 data/，队列不会被换代码清掉。
    """
    explicit = ""
    data_dir = ""
    if isinstance(cfg, dict):
        explicit = str(cfg.get("spool_dir") or "").strip()
        data_dir = str(cfg.get("data_dir") or "").strip()
    if explicit:
        return explicit
    if data_dir:
        return os.path.join(data_dir, "spool")
    return os.path.join("data", "spool")


def should_spool(code):
    """网络失败、超时和 5xx 才入队。4xx 是请求本身不对，重放也没用。"""
    try:
        status = int(code or 0)
    except (TypeError, ValueError):
        return True
    if status == 0 or status >= 500 or status in (408, 429):
        return True
    return False


def batch_url(metrics_url):
    url = str(metrics_url or "").rstrip("/")
    if url.endswith("/api/v1/metrics"):
        return url + "/batch"
    return url + "/batch"


def build_batch(spool, token=""):
    """取出一小批待补报样本。补报条目带 backfill，采集 timestamp 不改。"""
    samples = []
    for item in spool.peek_batch():
        sample = dict(item)
        sample.pop("token", None)
        sample["backfill"] = True
        if token:
            sample["token"] = token
        samples.append(sample)
    overflows = [dict(event) for event in spool.pending_overflows()]
    body = {"samples": samples, "overflows": overflows}
    if token:
        body["token"] = token
    return body


def parse_response(body):
    if isinstance(body, dict):
        return body
    if not body:
        return {}
    try:
        data = json.loads(body)
    except (TypeError, ValueError):
        return {}
    if isinstance(data, dict):
        return data
    return {}


def apply_flush_result(spool, sent_samples, sent_overflows, code, response):
    """2xx 才从队列删掉已发送的前缀，并记下中心给的保留小时。"""
    try:
        status = int(code or 0)
    except (TypeError, ValueError):
        status = 0
    if not (200 <= status < 300):
        return False
    spool.ack(sent_samples, sent_overflows)
    if isinstance(response, dict) and response.get("spool_retention_hours") is not None:
        spool.note_retention_hint(response.get("spool_retention_hours"))
    return True


class Spool(object):
    def __init__(self, directory, retention_hours=DEFAULT_RETENTION_HOURS, batch_size=DEFAULT_BATCH_SIZE, clock=None):
        self.directory = directory
        hours = coerce_retention_hours(retention_hours)
        self.retention_hours = hours if hours is not None else DEFAULT_RETENTION_HOURS
        try:
            size = int(batch_size)
        except (TypeError, ValueError):
            size = DEFAULT_BATCH_SIZE
        if size < 1:
            size = 1
        if size > MAX_BATCH_SIZE:
            size = MAX_BATCH_SIZE
        self.batch_size = size
        self._clock = clock
        os.makedirs(directory, exist_ok=True)
        self.queue_path = os.path.join(directory, "queue.jsonl")
        self.overflow_path = os.path.join(directory, "overflows.jsonl")
        self.hint_path = os.path.join(directory, "retention.json")

    def now(self):
        if self._clock is not None:
            return int(self._clock())
        return int(time.time())

    def effective_retention_hours(self):
        hint = self._read_hint()
        if hint is not None:
            return hint
        return int(self.retention_hours)

    def note_retention_hint(self, hours):
        """中心下发的保留小时盖过本机默认，并立刻按新界限丢掉过期样本。"""
        value = coerce_retention_hours(hours)
        if value is None:
            return False
        self._write_json(self.hint_path, {"spool_retention_hours": value})
        self._store_queue(self._read_jsonl(self.queue_path))
        return True

    def enqueue(self, payload):
        """写入一条未发出的样本。返回本次因保留时间丢掉样本而产生的溢出，没有则为 None。"""
        if not isinstance(payload, dict):
            raise ValueError("暂存样本必须是对象")
        item = dict(payload)
        item.pop("token", None)
        item.pop("backfill", None)
        if item.get("timestamp") in (None, ""):
            item["timestamp"] = self.now()
        items = self._read_jsonl(self.queue_path)
        items.append(item)
        _kept, overflow = self._store_queue(items)
        return overflow

    def pending_count(self):
        return len(self._read_jsonl(self.queue_path))

    def peek_batch(self):
        return self._read_jsonl(self.queue_path)[: self.batch_size]

    def pending_overflows(self):
        return self._read_jsonl(self.overflow_path)

    def ack(self, sample_count, overflow_count):
        """删掉已确认的前缀。调用方必须先按 peek 的顺序发送。"""
        try:
            samples = int(sample_count or 0)
        except (TypeError, ValueError):
            samples = 0
        try:
            overflows = int(overflow_count or 0)
        except (TypeError, ValueError):
            overflows = 0
        if samples > 0:
            items = self._read_jsonl(self.queue_path)
            self._write_jsonl(self.queue_path, items[samples:])
        if overflows > 0:
            items = self._read_jsonl(self.overflow_path)
            self._write_jsonl(self.overflow_path, items[overflows:])

    def _store_queue(self, items):
        kept, overflow = self._prune(items)
        self._write_jsonl(self.queue_path, kept)
        if overflow:
            self._append_overflow(overflow)
        return kept, overflow

    def _prune(self, items):
        cutoff = self.now() - int(self.effective_retention_hours()) * 3600
        kept = []
        dropped = []
        for item in items:
            ts = _timestamp(item)
            if ts is None or ts < cutoff:
                dropped.append(item)
            else:
                kept.append(item)
        if not dropped:
            return kept, None
        stamps = []
        for item in dropped:
            ts = _timestamp(item)
            if ts is not None:
                stamps.append(ts)
        if not stamps:
            stamps = [cutoff]
        return kept, {
            "from_ts": min(stamps),
            "to_ts": max(stamps),
            "dropped": len(dropped),
            "reason": "retention",
        }

    def _append_overflow(self, event):
        items = self._read_jsonl(self.overflow_path)
        items.append(dict(event))
        self._write_jsonl(self.overflow_path, items)

    def _read_hint(self):
        data = self._read_json(self.hint_path)
        if not isinstance(data, dict):
            return None
        return coerce_retention_hours(data.get("spool_retention_hours"))

    def _read_json(self, path):
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def _read_jsonl(self, path):
        if not os.path.isfile(path):
            return []
        items = []
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
                    item = json.loads(text)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        finally:
            handle.close()
        return items

    def _write_json(self, path, payload):
        self._atomic_write(path, json.dumps(payload, ensure_ascii=False))

    def _write_jsonl(self, path, items):
        lines = []
        for item in items:
            lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
        text = ""
        if lines:
            text = "\n".join(lines) + "\n"
        self._atomic_write(path, text)

    def _atomic_write(self, path, text):
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)


def _timestamp(item):
    if not isinstance(item, dict):
        return None
    raw = item.get("timestamp")
    if isinstance(raw, bool) or raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None
