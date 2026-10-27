"""管理 Token 与诊断报告推送配置。

管理 Token 只存 SHA-256，接口不再回显明文。
未设置时写接口与以前一样开放；设置之后，变更类接口必须带当前 Token。
Webhook 要能再次发送，所以保存在中心库里，读取接口只给打码后的地址。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import threading
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from center.report_push import CHANNEL_LABELS, SUPPORTED_CHANNELS


_TOKEN_KEY = "management_token_sha256"
_PUSH_ENABLED = "push_enabled"
_PUSH_CHANNEL = "push_channel"
_PUSH_WEBHOOK = "push_webhook_url"

_UPDATE_FIELDS = {
    "management_token",
    "clear_management_token",
    "push_enabled",
    "push_channel",
    "push_webhook_url",
    "clear_push_webhook",
}


def hash_management_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def validate_management_token(token: Any) -> Optional[str]:
    if not isinstance(token, str):
        return "管理 Token 必须是字符串"
    if not token or any(ch.isspace() for ch in token):
        return "管理 Token 不能包含空白"
    if len(token) < 8:
        return "管理 Token 至少 8 个字符"
    if len(token) > 128:
        return "管理 Token 不能超过 128 个字符"
    return None


def validate_webhook_url(url: Any) -> Optional[str]:
    if not isinstance(url, str):
        return "Webhook 必须是字符串"
    text = url.strip()
    if not text or any(ch.isspace() for ch in text):
        return "Webhook 不能包含空白"
    if len(text) > 2000:
        return "Webhook 过长"
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        return "Webhook 只接受 http 或 https"
    if not parsed.hostname or parsed.username or parsed.password:
        return "Webhook 地址无效"
    return None


def mask_webhook(url: str) -> str:
    """只留协议、主机和密钥末四位，避免设置页回显完整地址。"""
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    tail = ""
    query = parse_qs(parsed.query, keep_blank_values=False)
    for key in ("key", "access_token", "token"):
        values = query.get(key) or []
        if values and values[0]:
            secret = values[0]
            tail = "…" + (secret[-4:] if len(secret) >= 4 else secret)
            break
    if not tail:
        bare = (parsed.path or "").rstrip("/").split("/")[-1]
        if bare and bare not in ("send", "webhook"):
            tail = "…" + bare[-4:]
        elif len(url) >= 4:
            tail = "…" + url[-4:]
    if not host:
        return ""
    return "%s://%s/%s" % (parsed.scheme or "https", host, tail)


def _as_bool(value: Any) -> Optional[bool]:
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
    return None


def write_allowed(store: Optional["AdminSettingsStore"], presented: str) -> bool:
    """未配置管理 Token 时放行。配置后必须与保存的哈希一致。"""
    if store is None or not store.token_configured():
        return True
    return store.token_matches(presented or "")


def bootstrap_management_token(store: "AdminSettingsStore", raw: str) -> str:
    """仅在库里还没有管理 Token 时，用启动环境里的明文写入哈希。

    返回 absent / kept / applied / invalid:<原因>。不会把明文写进配置文件。
    """
    text = (raw or "").strip()
    if not text:
        return "absent"
    if store.token_configured():
        return "kept"
    err = validate_management_token(text)
    if err:
        return "invalid:" + err
    store.set_management_token(text)
    return "applied"


class AdminSettingsStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.Error:
            pass
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS admin_settings (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

    def _get(self, key: str) -> str:
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT value FROM admin_settings WHERE key=?", (key,)
                ).fetchone()
            finally:
                conn.close()
        if not row:
            return ""
        return str(row["value"] or "")

    def _sets(self, pairs: Dict[str, str], deletes: Tuple[str, ...] = ()) -> None:
        with self._lock:
            conn = self._connect()
            try:
                for key in deletes:
                    conn.execute("DELETE FROM admin_settings WHERE key=?", (key,))
                for key, value in pairs.items():
                    conn.execute(
                        """
                        INSERT INTO admin_settings(key, value) VALUES(?, ?)
                        ON CONFLICT(key) DO UPDATE SET value=excluded.value
                        """,
                        (key, value),
                    )
                conn.commit()
            finally:
                conn.close()

    def token_configured(self) -> bool:
        return bool(self._get(_TOKEN_KEY))

    def token_matches(self, presented: str) -> bool:
        stored = self._get(_TOKEN_KEY)
        if not stored:
            return True
        if len(stored) != 64 or not presented:
            return False
        digest = hash_management_token(presented)
        return hmac.compare_digest(digest, stored)

    def set_management_token(self, token: str) -> None:
        self._sets({_TOKEN_KEY: hash_management_token(token)})

    def clear_management_token(self) -> None:
        self._sets({}, deletes=(_TOKEN_KEY,))

    def push_config(self) -> Dict[str, Any]:
        """含 Webhook 原文，只给推送发送使用，不要放进 API 响应。"""
        channel = self._get(_PUSH_CHANNEL) or "wecom"
        if channel not in SUPPORTED_CHANNELS:
            channel = "wecom"
        return {
            "enabled": self._get(_PUSH_ENABLED) == "1",
            "channel": channel,
            "webhook_url": self._get(_PUSH_WEBHOOK).strip(),
        }

    def public_view(self) -> Dict[str, Any]:
        cfg = self.push_config()
        url = cfg["webhook_url"]
        return {
            "management_token_set": self.token_configured(),
            "push_enabled": bool(cfg["enabled"]),
            "push_channel": cfg["channel"],
            "push_channel_label": CHANNEL_LABELS.get(cfg["channel"], cfg["channel"]),
            "push_webhook_set": bool(url),
            "push_webhook_hint": mask_webhook(url) if url else "",
        }

    def update(self, data: Dict[str, Any]) -> Tuple[int, Dict[str, Any]]:
        if not isinstance(data, dict):
            return 400, {"ok": False, "error": "JSON 根节点必须是对象"}
        if not any(key in data for key in _UPDATE_FIELDS):
            return 400, {"ok": False, "error": "没有要保存的设置"}

        clear_token = data.get("clear_management_token", False)
        if "clear_management_token" in data and not isinstance(clear_token, bool):
            parsed = _as_bool(clear_token)
            if parsed is None:
                return 400, {"ok": False, "error": "clear_management_token 只能是 true 或 false"}
            clear_token = parsed
        has_token = "management_token" in data
        if has_token and clear_token:
            return 400, {"ok": False, "error": "不能同时设置和清空管理 Token"}

        new_token = ""
        if has_token:
            err = validate_management_token(data.get("management_token"))
            if err:
                return 400, {"ok": False, "error": err}
            new_token = str(data.get("management_token"))

        clear_webhook = data.get("clear_push_webhook", False)
        if "clear_push_webhook" in data and not isinstance(clear_webhook, bool):
            parsed = _as_bool(clear_webhook)
            if parsed is None:
                return 400, {"ok": False, "error": "clear_push_webhook 只能是 true 或 false"}
            clear_webhook = parsed
        webhook = ""
        if "push_webhook_url" in data:
            if clear_webhook:
                return 400, {"ok": False, "error": "不能同时填写和清空 Webhook"}
            raw_url = data.get("push_webhook_url")
            if raw_url in ("", None):
                return 400, {"ok": False, "error": "留空不会改 Webhook，清空请明确提交 clear_push_webhook"}
            err = validate_webhook_url(raw_url)
            if err:
                return 400, {"ok": False, "error": err}
            webhook = str(raw_url).strip()

        channel = None
        if "push_channel" in data:
            channel = str(data.get("push_channel") or "").strip().lower()
            if channel not in SUPPORTED_CHANNELS:
                return 400, {
                    "ok": False,
                    "error": "推送渠道只能是 wecom、dingtalk 或 feishu",
                }

        enabled = None
        if "push_enabled" in data:
            enabled = _as_bool(data.get("push_enabled"))
            if enabled is None:
                return 400, {"ok": False, "error": "push_enabled 只能是 true 或 false"}

        token_action = bool(has_token or clear_token)
        push_action = (
            "push_enabled" in data
            or "push_channel" in data
            or "push_webhook_url" in data
            or bool(clear_webhook)
        )
        if not token_action and not push_action:
            return 400, {"ok": False, "error": "没有要保存的设置"}

        pairs: Dict[str, str] = {}
        deletes = []
        if has_token:
            pairs[_TOKEN_KEY] = hash_management_token(new_token)
        elif clear_token:
            deletes.append(_TOKEN_KEY)
        if channel is not None:
            pairs[_PUSH_CHANNEL] = channel
        if enabled is not None:
            pairs[_PUSH_ENABLED] = "1" if enabled else "0"
        if webhook:
            pairs[_PUSH_WEBHOOK] = webhook
        elif clear_webhook:
            deletes.append(_PUSH_WEBHOOK)
            if enabled is None:
                pairs[_PUSH_ENABLED] = "0"

        self._sets(pairs, deletes=tuple(deletes))
        return 200, {"ok": True, "settings": self.public_view()}


def handle_get_admin_settings(store: Optional[AdminSettingsStore]) -> Tuple[int, Dict[str, Any]]:
    if store is None:
        return 503, {"ok": False, "error": "管理设置未初始化"}
    return 200, {"ok": True, "settings": store.public_view()}


def handle_save_admin_settings(
    store: Optional[AdminSettingsStore], body: bytes
) -> Tuple[int, Dict[str, Any]]:
    if store is None:
        return 503, {"ok": False, "error": "管理设置未初始化"}
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "请求体必须是合法 JSON"}
    return store.update(data)
