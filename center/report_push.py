"""把诊断报告发到企业微信、钉钉或飞书机器人。

未启用、或没有 Webhook 时不发任何请求。
失败只返回说明，不把 Webhook 地址写进错误信息。
传输失败会再试两次；对方明确拒绝则不重试。
测试发送只发一条短消息，不生成诊断报告。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


SUPPORTED_CHANNELS = ("wecom", "dingtalk", "feishu")
CHANNEL_LABELS = {
    "wecom": "企业微信",
    "dingtalk": "钉钉",
    "feishu": "飞书",
}
KIND_LABELS = {
    "test": "测试发送",
    "report": "报告推送",
}

_MAX_CHARS = 3500
_TIMEOUT_SECONDS = 10
_RETRY_BACKOFF_SECONDS = (0.4, 0.8)
SKIPPED_MESSAGE = "未启用推送或没有 Webhook"
TEST_TITLE = "诊断报告推送测试"
TEST_TEXT = "【诊断报告推送测试】这是一条测试消息，用来确认机器人能收到。未生成新的诊断报告。"

_store = None
SHANGHAI = timezone(timedelta(hours=8))


def set_push_store(store: Any) -> None:
    global _store
    _store = store


def get_push_store() -> Any:
    return _store


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def format_shanghai(moment: datetime) -> str:
    return moment.astimezone(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def scrub_webhook_text(text: str, url: str) -> str:
    """错误说明里不能留下 Webhook 或密钥。"""
    cleaned = str(text or "")
    secret_url = str(url or "").strip()
    if not cleaned or not secret_url:
        return cleaned
    if secret_url in cleaned:
        cleaned = cleaned.replace(secret_url, "（地址已隐藏）")
    parsed = urlparse(secret_url)
    if parsed.query and parsed.query in cleaned:
        cleaned = cleaned.replace(parsed.query, "（地址已隐藏）")
    secrets = []
    query = parse_qs(parsed.query, keep_blank_values=False)
    for key in ("key", "access_token", "token"):
        secrets.extend(query.get(key) or [])
    bare = (parsed.path or "").rstrip("/").split("/")[-1]
    if bare and bare not in ("send", "webhook") and len(bare) >= 8:
        secrets.append(bare)
    for secret in secrets:
        if secret and len(secret) >= 4 and secret in cleaned:
            cleaned = cleaned.replace(secret, "（密钥已隐藏）")
    return cleaned


def _source_label(source: Any) -> str:
    if source == "scheduled":
        return "定时"
    if source == "manual":
        return "手动"
    return ""


def build_report_text(report: Dict[str, Any]) -> str:
    title = str(report.get("title") or "").strip() or "使用诊断报告"
    lines = ["【诊断报告】%s" % title]
    source = _source_label(report.get("source"))
    if source:
        lines.append("来源：%s" % source)
    markdown = str(report.get("markdown") or "").strip()
    if markdown:
        lines.append("")
        lines.append(markdown)
    text = "\n".join(lines)
    if len(text) <= _MAX_CHARS:
        return text
    note = "\n\n……正文已截断，请到监测台「使用诊断」查看全文。"
    keep = max(0, _MAX_CHARS - len(note))
    return text[:keep].rstrip() + note


def build_payload(channel: str, report: Dict[str, Any]) -> Dict[str, Any]:
    text = build_report_text(report)
    title = str(report.get("title") or "诊断报告").strip() or "诊断报告"
    if len(title) > 120:
        title = title[:120]
    return _channel_payload(channel, title, text)


def build_test_payload(channel: str) -> Dict[str, Any]:
    """短消息，正文含「诊断」。不带加签字段，也不带报告正文。"""
    return _channel_payload(channel, TEST_TITLE, TEST_TEXT)


def _channel_payload(channel: str, title: str, text: str) -> Dict[str, Any]:
    if channel == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": text}}
    if channel == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    raise ValueError("未知推送渠道")


def push_summary(view: Dict[str, Any]) -> str:
    kind = str(view.get("kind_label") or "报告推送")
    when = str(view.get("at") or "")
    channel = str(view.get("channel_label") or "")
    channel_bit = (" · " + channel) if channel else ""
    if view.get("skipped"):
        why = str(view.get("message") or SKIPPED_MESSAGE)
        return "最近一次：%s未发出%s · %s · %s" % (kind, channel_bit, when, why)
    if view.get("ok"):
        return "最近一次：%s成功%s · %s" % (kind, channel_bit, when)
    why = str(view.get("error") or "推送失败")
    return "最近一次：%s失败%s · %s · %s" % (kind, channel_bit, when, why)


def public_push_result(result: Dict[str, Any], kind: str = "report", url: str = "") -> Dict[str, Any]:
    """给接口和设置页的推送结果。只留白名单字段。"""
    raw_kind = result.get("kind") if result.get("kind") in KIND_LABELS else kind
    kind_norm = raw_kind if raw_kind in KIND_LABELS else "report"
    channel = str(result.get("channel") or "")
    if channel not in SUPPORTED_CHANNELS:
        channel = ""
    reason = "not_configured" if result.get("reason") == "not_configured" else ""
    message = scrub_webhook_text(str(result.get("message") or ""), url)
    if result.get("skipped") and not message:
        message = SKIPPED_MESSAGE
    at = str(result.get("at") or "") or format_shanghai(now_shanghai())
    view = {
        "ok": bool(result.get("ok")),
        "skipped": bool(result.get("skipped")),
        "kind": kind_norm,
        "kind_label": KIND_LABELS[kind_norm],
        "at": at,
        "channel": channel,
        "channel_label": CHANNEL_LABELS.get(channel, "") if channel else "",
        "error": scrub_webhook_text(str(result.get("error") or ""), url),
        "reason": reason,
        "message": message,
    }
    view["summary"] = scrub_webhook_text(push_summary(view), url)
    return view


def remember_push_result(store: Any, result: Dict[str, Any], kind: str = "report") -> Dict[str, Any]:
    url = ""
    if store is not None and hasattr(store, "push_config"):
        try:
            url = str((store.push_config() or {}).get("webhook_url") or "")
        except Exception:  # noqa: BLE001
            url = ""
    stamped = dict(result)
    stamped["kind"] = "test" if kind == "test" else "report"
    view = public_push_result(stamped, stamped["kind"], url)
    if store is not None and hasattr(store, "record_push_receipt"):
        return store.record_push_receipt(view)
    return view


def _vendor_error(channel: str, raw: str) -> Optional[str]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return "推送接口返回的不是 JSON"
    if not isinstance(data, dict):
        return "推送接口返回异常"
    if channel in ("wecom", "dingtalk"):
        if "errcode" not in data and "code" not in data:
            return "推送接口没有返回结果码"
        code = data.get("errcode", data.get("code"))
        if code in (0, "0"):
            return None
        message = data.get("errmsg") or data.get("msg") or "推送被拒绝"
        return "推送被拒绝：%s" % message
    code = data.get("code", data.get("StatusCode"))
    if code in (0, "0"):
        return None
    message = data.get("msg") or data.get("StatusMessage") or "推送被拒绝"
    return "推送被拒绝：%s" % message


def _post_json(url: str, payload: Dict[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            detail = ""
        if detail.strip():
            return detail
        raise URLError("http")


def _post_with_retry(url: str, payload: Dict[str, Any]) -> str:
    """传输失败再试两次。不把地址写进异常文本之外的返回值。"""
    attempts = 1 + len(_RETRY_BACKOFF_SECONDS)
    last_error: Optional[BaseException] = None
    for index in range(attempts):
        try:
            return _post_json(url, payload)
        except (URLError, OSError) as exc:
            last_error = exc
            if index >= len(_RETRY_BACKOFF_SECONDS):
                break
            time.sleep(_RETRY_BACKOFF_SECONDS[index])
    if last_error is not None:
        raise last_error
    raise URLError("push")


def push_report(
    report: Optional[Dict[str, Any]] = None,
    store: Any = None,
    kind: str = "report",
) -> Dict[str, Any]:
    """按当前配置发送。未配置时 skipped，不访问网络。

    kind 为 test 时发短消息，不使用报告正文，也不生成报告。
    """
    target = store if store is not None else get_push_store()
    send_kind = "test" if kind == "test" else "report"
    if target is None:
        return public_push_result(
            {
                "ok": True,
                "skipped": True,
                "reason": "not_configured",
                "message": SKIPPED_MESSAGE,
            },
            send_kind,
        )
    cfg = target.push_config()
    url = str(cfg.get("webhook_url") or "").strip()
    enabled = bool(cfg.get("enabled"))
    channel = str(cfg.get("channel") or "")
    if channel not in SUPPORTED_CHANNELS:
        channel = ""
    label = CHANNEL_LABELS.get(channel, "")
    if not enabled or not url or not channel:
        return remember_push_result(
            target,
            {
                "ok": True,
                "skipped": True,
                "reason": "not_configured",
                "message": SKIPPED_MESSAGE,
                "channel": channel,
                "channel_label": label,
            },
            send_kind,
        )
    try:
        payload = build_test_payload(channel) if send_kind == "test" else build_payload(channel, report or {})
    except ValueError:
        return remember_push_result(
            target,
            {"ok": False, "skipped": False, "error": "推送渠道无效", "channel": channel},
            send_kind,
        )
    try:
        raw = _post_with_retry(url, payload)
    except (URLError, OSError, ValueError):
        return remember_push_result(
            target,
            {
                "ok": False,
                "skipped": False,
                "error": "推送请求失败",
                "channel": channel,
                "channel_label": label,
            },
            send_kind,
        )
    vendor = _vendor_error(channel, raw)
    if vendor:
        return remember_push_result(
            target,
            {
                "ok": False,
                "skipped": False,
                "error": scrub_webhook_text(vendor, url),
                "channel": channel,
                "channel_label": label,
            },
            send_kind,
        )
    return remember_push_result(
        target,
        {
            "ok": True,
            "skipped": False,
            "channel": channel,
            "channel_label": label,
        },
        send_kind,
    )
