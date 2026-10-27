#!/usr/bin/env python3
"""管理 Token、设置持久化，以及诊断报告推送。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.admin_settings import (
    AdminSettingsStore,
    bootstrap_management_token,
    hash_management_token,
)
from center.report_push import build_payload, push_report, set_push_store
from center.report_schedule import ReportScheduler, deliver_report
from center.server import make_handler
from center.storage import Storage


TOKEN = "mgmt-token-plain-9f3a"
OTHER = "rotated-token-77"
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key-abcdef"
SHANGHAI = timezone(timedelta(hours=8))


def _ts(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=SHANGHAI).timestamp())


class _Resp:
    def __init__(self, raw: str) -> None:
        self._raw = raw.encode("utf-8")
        self.status = 200

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _request(method: str, url: str, body: dict = None, headers: dict = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    hdrs = dict(headers or {})
    if body is not None:
        hdrs.setdefault("Content-Type", "application/json")
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, json.loads(raw) if raw else {}


class SettingsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "monitor.db")
        self.store = AdminSettingsStore(self.db)
        set_push_store(None)

    def tearDown(self) -> None:
        set_push_store(None)
        self.tmp.cleanup()

    def test_token_roundtrip_hides_plaintext_and_survives_reopen(self) -> None:
        self.assertFalse(self.store.token_configured())
        code, payload = self.store.update({"management_token": TOKEN})
        self.assertEqual(code, 200, payload)
        view = payload["settings"]
        self.assertTrue(view["management_token_set"])
        self.assertNotIn(TOKEN, json.dumps(view, ensure_ascii=False))
        self.assertNotIn("management_token_sha256", json.dumps(view))
        self.assertTrue(self.store.token_matches(TOKEN))
        self.assertFalse(self.store.token_matches("wrong-token"))
        self.assertFalse(self.store.token_matches(""))

        again = AdminSettingsStore(self.db)
        self.assertTrue(again.token_matches(TOKEN))
        self.assertFalse(again.token_matches(OTHER))
        conn = sqlite3.connect(self.db)
        try:
            rows = conn.execute("SELECT key, value FROM admin_settings").fetchall()
        finally:
            conn.close()
        blob = "\n".join("%s=%s" % (key, value) for key, value in rows)
        self.assertNotIn(TOKEN, blob)
        self.assertIn(hash_management_token(TOKEN), blob)

        code, payload = again.update({"management_token": OTHER})
        self.assertEqual(code, 200, payload)
        self.assertFalse(again.token_matches(TOKEN))
        self.assertTrue(again.token_matches(OTHER))

        code, payload = again.update({"clear_management_token": True})
        self.assertEqual(code, 200, payload)
        self.assertFalse(payload["settings"]["management_token_set"])
        self.assertTrue(again.token_matches("anything-open"))

    def test_reject_short_token_blank_webhook_and_bad_channel(self) -> None:
        code, payload = self.store.update({"management_token": "short"})
        self.assertEqual(code, 400, payload)
        self.assertFalse(self.store.token_configured())
        code, payload = self.store.update({"management_token": TOKEN, "clear_management_token": True})
        self.assertEqual(code, 400, payload)
        code, payload = self.store.update({"push_channel": "email"})
        self.assertEqual(code, 400, payload)
        code, payload = self.store.update({"push_webhook_url": "file:///tmp/hook"})
        self.assertEqual(code, 400, payload)
        code, payload = self.store.update({"push_webhook_url": ""})
        self.assertEqual(code, 400, payload)
        self.assertFalse(self.store.public_view()["push_webhook_set"])

    def test_push_settings_mask_webhook_and_keep_when_omitted(self) -> None:
        code, payload = self.store.update(
            {
                "push_enabled": True,
                "push_channel": "wecom",
                "push_webhook_url": WEBHOOK,
            }
        )
        self.assertEqual(code, 200, payload)
        view = payload["settings"]
        self.assertTrue(view["push_enabled"])
        self.assertEqual(view["push_channel"], "wecom")
        self.assertEqual(view["push_channel_label"], "企业微信")
        self.assertTrue(view["push_webhook_set"])
        self.assertNotIn("secret-key-abcdef", json.dumps(view))
        self.assertIn("cdef", view["push_webhook_hint"])
        self.assertNotIn("secret-key", view["push_webhook_hint"])

        code, payload = self.store.update({"push_enabled": False, "push_channel": "feishu"})
        self.assertEqual(code, 200, payload)
        self.assertEqual(self.store.push_config()["webhook_url"], WEBHOOK)
        self.assertEqual(self.store.public_view()["push_channel"], "feishu")
        self.assertFalse(self.store.public_view()["push_enabled"])

        code, payload = self.store.update({"clear_push_webhook": True})
        self.assertEqual(code, 200, payload)
        self.assertFalse(self.store.public_view()["push_webhook_set"])
        self.assertEqual(self.store.push_config()["webhook_url"], "")
        self.assertFalse(self.store.public_view()["push_enabled"])

    def test_bootstrap_applies_once_and_rejects_short_env(self) -> None:
        self.assertEqual(bootstrap_management_token(self.store, ""), "absent")
        self.assertEqual(bootstrap_management_token(self.store, "short"), "invalid:管理 Token 至少 8 个字符")
        self.assertFalse(self.store.token_configured())
        self.assertEqual(bootstrap_management_token(self.store, TOKEN), "applied")
        self.assertTrue(self.store.token_matches(TOKEN))
        self.assertEqual(bootstrap_management_token(self.store, OTHER), "kept")
        self.assertTrue(self.store.token_matches(TOKEN))
        self.assertFalse(self.store.token_matches(OTHER))


class PushDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = AdminSettingsStore(os.path.join(self.tmp.name, "monitor.db"))
        set_push_store(None)
        self.report = {
            "id": 7,
            "title": "使用诊断日报",
            "source": "scheduled",
            "status": "ok",
            "markdown": "集群 CPU 平均 12%。",
        }

    def tearDown(self) -> None:
        set_push_store(None)
        self.tmp.cleanup()

    def test_unconfigured_is_noop(self) -> None:
        with mock.patch("center.report_push.urlopen") as opener:
            result = deliver_report(self.report)
        self.assertTrue(result["ok"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "not_configured")
        opener.assert_not_called()

        self.store.update({"push_webhook_url": WEBHOOK, "push_enabled": False, "push_channel": "wecom"})
        with mock.patch("center.report_push.urlopen") as opener:
            result = push_report(self.report, self.store)
        opener.assert_not_called()
        self.assertTrue(result["skipped"])

    def test_wecom_dingtalk_feishu_payloads_and_vendor_error(self) -> None:
        self.store.update(
            {"push_enabled": True, "push_channel": "wecom", "push_webhook_url": WEBHOOK}
        )
        with mock.patch("center.report_push.urlopen", return_value=_Resp('{"errcode":0,"errmsg":"ok"}')) as opener:
            result = push_report(self.report, self.store)
        self.assertTrue(result["ok"], result)
        self.assertFalse(result["skipped"])
        req = opener.call_args[0][0]
        self.assertEqual(req.full_url, WEBHOOK)
        body = json.loads(req.data.decode("utf-8"))
        self.assertEqual(body["msgtype"], "markdown")
        self.assertIn("集群 CPU 平均 12%", body["markdown"]["content"])
        self.assertIn("诊断报告", body["markdown"]["content"])
        self.assertNotIn(WEBHOOK, json.dumps(result))

        dingtalk = build_payload("dingtalk", self.report)
        self.assertEqual(dingtalk["msgtype"], "markdown")
        self.assertIn("诊断", dingtalk["markdown"]["text"])
        feishu = build_payload("feishu", {"title": "月报", "markdown": "正文", "source": "manual"})
        self.assertEqual(feishu["msg_type"], "text")
        self.assertIn("来源：手动", feishu["content"]["text"])

        long_report = dict(self.report, markdown="甲" * 5000)
        text = build_payload("wecom", long_report)["markdown"]["content"]
        self.assertLessEqual(len(text), 3500)
        self.assertIn("正文已截断", text)

        with mock.patch(
            "center.report_push.urlopen",
            return_value=_Resp('{"errcode":93000,"errmsg":"invalid webhook"}'),
        ):
            failed = push_report(self.report, self.store)
        self.assertFalse(failed["ok"])
        self.assertIn("推送被拒绝", failed["error"])
        self.assertNotIn(WEBHOOK, failed["error"])

    def test_deliver_swallows_unexpected_errors(self) -> None:
        with mock.patch("center.report_push.push_report", side_effect=RuntimeError("boom-secret")):
            result = deliver_report(self.report)
        self.assertFalse(result["ok"])
        self.assertNotIn("boom-secret", json.dumps(result))

    def test_scheduler_sends_when_configured(self) -> None:
        self.store.update(
            {"push_enabled": True, "push_channel": "dingtalk", "push_webhook_url": WEBHOOK}
        )
        set_push_store(self.store)
        storage = Storage(os.path.join(self.tmp.name, "metrics.db"), offline_seconds=90, retention_days=40)
        fire = _ts(2026, 10, 5, 0, 10)
        sched = ReportScheduler(storage, clock=lambda: fire)
        with mock.patch("center.report_push.urlopen", return_value=_Resp('{"errcode":0}')) as opener:
            created = sched.run_due(fire)
        self.assertEqual(created[0]["report"]["status"], "ok")
        self.assertEqual(opener.call_count, 1)
        sent = json.loads(opener.call_args[0][0].data.decode("utf-8"))
        self.assertEqual(sent["msgtype"], "markdown")
        self.assertIn("使用诊断", sent["markdown"]["text"])


class ManagementGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "monitor.db")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=40)
        self.admin = AdminSettingsStore(self.db)
        set_push_store(self.admin)
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(self.storage, "agent-secret", admin_store=self.admin),
        )
        self.base = "http://127.0.0.1:%s" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        set_push_store(None)
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def test_gate_settings_page_and_manual_push(self) -> None:
        code, page_status = _request("GET", self.base + "/api/v1/settings")
        self.assertEqual(code, 200, page_status)
        self.assertFalse(page_status["settings"]["management_token_set"])

        code, created = _request("POST", self.base + "/api/v1/groups", {"name": "开放时"})
        self.assertEqual(code, 200, created)

        with urlopen(self.base + "/settings.html") as resp:
            html = resp.read().decode("utf-8")
        self.assertIn("管理 Token", html)
        self.assertIn("企业微信", html)
        self.assertIn("钉钉", html)
        self.assertIn("飞书", html)
        with urlopen(self.base + "/") as resp:
            index = resp.read().decode("utf-8")
        self.assertIn("/settings.html", index)
        self.assertIn("/admin.js", index)

        code, saved = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"management_token": TOKEN},
        )
        self.assertEqual(code, 200, saved)
        self.assertTrue(saved["settings"]["management_token_set"])
        self.assertNotIn(TOKEN, json.dumps(saved))

        code, denied = _request("POST", self.base + "/api/v1/groups", {"name": "应拒绝"})
        self.assertEqual(code, 401, denied)
        self.assertEqual(denied["code"], "management_token_required")
        code, wrong = _request(
            "POST",
            self.base + "/api/v1/groups",
            {"name": "错误口令"},
            headers={"Content-Type": "application/json", "X-Management-Token": "not-the-token"},
        )
        self.assertEqual(code, 401, wrong)

        code, hosts = _request("GET", self.base + "/api/v1/hosts")
        self.assertEqual(code, 200, hosts)
        code, settings = _request("GET", self.base + "/api/v1/settings")
        self.assertEqual(code, 200, settings)

        metric = {
            "host_id": "h-1",
            "hostname": "h-1",
            "host_type": "cpu",
            "timestamp": _ts(2026, 10, 5, 1, 0),
            "token": "agent-secret",
            "system": {"cpu_percent": 10, "mem_percent": 10, "disk_percent": 10},
        }
        code, ingested = _request("POST", self.base + "/api/v1/metrics", metric)
        self.assertEqual(code, 200, ingested)
        metric["token"] = TOKEN
        code, rejected = _request("POST", self.base + "/api/v1/metrics", metric)
        self.assertEqual(code, 401, rejected)
        self.assertNotEqual(rejected.get("code"), "management_token_required")

        code, deploy = _request("POST", self.base + "/api/v1/deploy/settings", {})
        self.assertEqual(code, 401, deploy)

        auth = {"Content-Type": "application/json", "X-Management-Token": TOKEN}
        code, allowed = _request(
            "POST", self.base + "/api/v1/groups", {"name": "凭据通过"}, headers=auth
        )
        self.assertEqual(code, 200, allowed)

        self.admin.update(
            {"push_enabled": True, "push_channel": "wecom", "push_webhook_url": WEBHOOK}
        )
        with mock.patch("center.report_push.urlopen") as opener:
            code, blocked = _request(
                "POST",
                self.base + "/api/v1/reports/generate",
                {"period": "day", "as_of": _ts(2026, 10, 5, 12, 0)},
            )
        self.assertEqual(code, 401, blocked)
        opener.assert_not_called()

        with mock.patch(
            "center.report_push.urlopen",
            return_value=_Resp('{"errcode":0,"errmsg":"ok"}'),
        ) as opener:
            code, generated = _request(
                "POST",
                self.base + "/api/v1/reports/generate",
                {"period": "day", "as_of": _ts(2026, 10, 5, 12, 0)},
                headers=auth,
            )
        self.assertEqual(code, 200, generated)
        self.assertEqual(generated["report"]["source"], "manual")
        self.assertFalse(generated["push"]["skipped"])
        self.assertEqual(generated["push"]["channel"], "wecom")
        self.assertEqual(opener.call_count, 1)
        self.assertNotIn("secret-key-abcdef", json.dumps(generated["push"]))

        code, cleared = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"clear_management_token": True},
            headers=auth,
        )
        self.assertEqual(code, 200, cleared)
        self.assertFalse(cleared["settings"]["management_token_set"])
        code, opened = _request("POST", self.base + "/api/v1/groups", {"name": "清空后开放"})
        self.assertEqual(code, 200, opened)
