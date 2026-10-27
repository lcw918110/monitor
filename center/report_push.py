"""把诊断报告发到企业微信、钉钉或飞书机器人。

未启用、或没有 Webhook 时不发任何请求。
失败只返回说明，不把 Webhook 地址写进错误信息。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SUPPORTED_CHANNELS = ("wecom", "dingtalk", "feishu")
CHANNEL_LABELS = {
    "wecom": "企业微信",
    "dingtalk": "钉钉",
    "feishu": "飞书",
}

_MAX_CHARS = 3500
_TIMEOUT_SECONDS = 10

_store = None


def set_push_store(store: Any) -> None:
    global _store
    _store = store


def get_push_store() -> Any:
    return _store


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
    if channel == "wecom":
        return {"msgtype": "markdown", "markdown": {"content": text}}
    if channel == "dingtalk":
        return {"msgtype": "markdown", "markdown": {"title": title, "text": text}}
    if channel == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    raise ValueError("未知推送渠道")


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


def push_report(report: Dict[str, Any], store: Any = None) -> Dict[str, Any]:
    """按当前配置发送。未配置时 skipped，不访问网络。"""
    target = store if store is not None else get_push_store()
    if target is None:
        return {"ok": True, "skipped": True, "reason": "not_configured"}
    cfg = target.push_config()
    url = str(cfg.get("webhook_url") or "").strip()
    enabled = bool(cfg.get("enabled"))
    channel = str(cfg.get("channel") or "")
    if not enabled or not url:
        return {"ok": True, "skipped": True, "reason": "not_configured"}
    if channel not in SUPPORTED_CHANNELS:
        return {"ok": False, "skipped": False, "error": "推送渠道无效"}
    try:
        payload = build_payload(channel, report or {})
        raw = _post_json(url, payload)
    except (URLError, TimeoutError, OSError, ValueError):
        return {"ok": False, "skipped": False, "error": "推送请求失败"}
    vendor = _vendor_error(channel, raw)
    if vendor:
        return {"ok": False, "skipped": False, "error": vendor, "channel": channel}
    return {
        "ok": True,
        "skipped": False,
        "channel": channel,
        "channel_label": CHANNEL_LABELS.get(channel, channel),
    }
