#!/usr/bin/env python3
"""中心参数可视化配置：目录、默认值、写回文件，以及报告/着色/清理读新值。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.admin_settings import AdminSettingsStore
from center.center_params import (
    FIELDS,
    GROUPS,
    ANOMALY_ALIASES,
    CenterRuntime,
    merge_center_config,
    period_util_thresholds,
)
from center.report_schedule import ReportScheduler
from center.reports import (
    HIGH_BUSY_GTE,
    HIGH_P95_GTE,
    IDLE_AVG_LT,
    IDLE_BUSY_LT,
    REPORT_MIN_SAMPLES,
    idle_status,
    report_period_bounds,
    resolve_report_rules,
)
from center.server import CleanupControl, make_handler
from center.storage import Storage
from common.validate import validate_center_config


SHANGHAI = timezone(timedelta(hours=8))
MGMT = "mgmt-token-plain-9f3a"
AGENT = "agent-secret-xyz9"
WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=secret-key-abcdef"


def _request(method, url, body=None, headers=None):
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


def _leaves(obj, prefix=""):
    found = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            path = ("%s.%s" % (prefix, key)) if prefix else key
            if isinstance(value, dict):
                found.extend(_leaves(value, path))
            else:
                found.append(path)
    return found


def _field_map(view):
    found = {}
    for group in view["groups"]:
        for field in group["fields"]:
            found[field["key"]] = field
    return found


def _host(avg, busy, online=True, cpu_n=12, p95=None):
    return {
        "online": online,
        "accel_count": 0,
        "metrics": {
            "cpu_percent": {
                "avg": avg,
                "busy_ratio": busy,
                "p95": avg if p95 is None else p95,
                "sample_count": cpu_n,
            }
        },
    }


class CatalogTests(unittest.TestCase):
    def test_example_leaves_are_configurable(self) -> None:
        with open(os.path.join(ROOT, "config", "center.example.json"), encoding="utf-8") as handle:
            example = json.load(handle)
        missing = [key for key in _leaves(example) if key not in FIELDS]
        self.assertEqual(missing, [])
        for key in (
            "period_util.busy_mem_percent",
            "period_util.busy_disk_percent",
            "report.idle_avg_lt",
            "report.idle_busy_lt",
            "report.high_busy_gte",
            "report.high_p95_gte",
            "report.min_samples",
        ):
            self.assertIn(key, FIELDS)

    def test_defaults_match_code_and_descriptions_are_human(self) -> None:
        self.assertEqual(FIELDS["report.idle_avg_lt"][1]["default"], IDLE_AVG_LT)
        self.assertEqual(FIELDS["report.idle_busy_lt"][1]["default"], IDLE_BUSY_LT)
        self.assertEqual(FIELDS["report.high_busy_gte"][1]["default"], HIGH_BUSY_GTE)
        self.assertEqual(FIELDS["report.high_p95_gte"][1]["default"], HIGH_P95_GTE)
        self.assertEqual(FIELDS["report.min_samples"][1]["default"], REPORT_MIN_SAMPLES)
        for key, (_group, field) in FIELDS.items():
            text = field["description"]
            self.assertIn("影响", text, key)
            self.assertGreater(len(text), 24, key)
            self.assertNotEqual(text, key)
            if field["kind"] in ("number", "optional_number"):
                self.assertIn("单位", text, key)

    def test_frequent_items_come_first(self) -> None:
        ids = [group["id"] for group in GROUPS]
        self.assertEqual(
            ids,
            ["retention", "anomaly", "report", "period", "schedule", "security", "service"],
        )
        self.assertEqual(GROUPS[0]["fields"][0]["key"], "retention_days")
        retention = [field["key"] for field in GROUPS[0]["fields"]]
        self.assertLess(retention.index("retention_days"), retention.index("cleanup_interval_seconds"))
        anomaly = [field["key"] for field in GROUPS[1]["fields"]]
        self.assertEqual(anomaly[0], "anomaly.cpu_load_per_core_warn")
        self.assertLess(
            anomaly.index("anomaly.cpu_load_per_core_warn"),
            anomaly.index("anomaly.accel_mem_critical_percent"),
        )
        report = [field["key"] for field in GROUPS[2]["fields"]]
        self.assertEqual(report[0], "report.idle_avg_lt")
        self.assertLess(report.index("report.high_busy_gte"), report.index("report.min_samples"))
        self.assertEqual(GROUPS[-1]["id"], "service")
        self.assertTrue(GROUPS[-1]["fields"][0].get("restart"))

    def test_missing_report_uses_defaults_and_alias_overrides_accel(self) -> None:
        cfg = merge_center_config({"token": "keep-me", "retention_days": 40})
        self.assertEqual(cfg["token"], "keep-me")
        self.assertEqual(cfg["retention_days"], 40)
        self.assertEqual(cfg["report"]["idle_avg_lt"], 5.0)
        self.assertEqual(cfg["report"]["idle_busy_lt"], 0.05)
        ok, errors = validate_center_config(cfg)
        self.assertTrue(ok, errors)

        aliased = merge_center_config({"anomaly": {"npu_temp_warn_c": 70}})
        self.assertEqual(aliased["anomaly"]["accel_temp_warn_c"], 70)
        both = merge_center_config(
            {"anomaly": {"npu_temp_warn_c": 70, "accel_temp_warn_c": 60}}
        )
        self.assertEqual(both["anomaly"]["accel_temp_warn_c"], 60)

        bad = merge_center_config({"report": {"idle_avg_lt": 200}})
        ok, errors = validate_center_config(bad)
        self.assertFalse(ok)
        self.assertTrue(any("idle_avg_lt" in err for err in errors))

    def test_rules_change_idle_classification(self) -> None:
        self.assertFalse(idle_status(_host(14.0, 0.01))[0])
        loose = resolve_report_rules(
            {"idle_avg_lt": 20, "idle_busy_lt": 0.5, "min_samples": 1}
        )
        ok, reason = idle_status(_host(14.0, 0.01, cpu_n=1), min_samples=1, rules=loose)
        self.assertTrue(ok, reason)
        self.assertIn("20", reason)
        tight = resolve_report_rules({"high_busy_gte": 0.1, "high_p95_gte": 99})
        from center.reports import high_load_reasons

        self.assertTrue(high_load_reasons(_host(10, 0.1, p95=20), rules=tight))
        self.assertFalse(high_load_reasons(_host(10, 0.09, p95=20), rules=tight))

    def test_notes_file_mentions_report_ratios(self) -> None:
        path = os.path.join(ROOT, "config", "center.example.md")
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("idle_busy_lt", text)
        self.assertIn("0.05", text)
        self.assertIn("high_busy_gte", text)
        with open(os.path.join(ROOT, "center", "static", "settings.html"), encoding="utf-8") as handle:
            html = handle.read()
        with open(os.path.join(ROOT, "center", "static", "settings.js"), encoding="utf-8") as handle:
            script = handle.read()
        self.assertIn("centerLead", html)
        self.assertIn("管理 Token", html)
        self.assertIn("企业微信", html)
        self.assertIn("钉钉", html)
        self.assertIn("飞书", html)
        self.assertIn("恢复默认", script)
        self.assertIn("启用推送", html)


class RuntimePersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "center.json")
        self.db = os.path.join(self.tmp.name, "monitor.db")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=7)
        self.scheduler = ReportScheduler(self.storage)
        self.cleanup = CleanupControl(3600)
        self.runtime = CenterRuntime(
            {"custom_flag": True, "alerts": {"legacy": 1}, "retention_days": 9},
            self.path,
            storage=self.storage,
            scheduler=self.scheduler,
            cleanup=self.cleanup,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_save_restore_and_keep_unknown_keys(self) -> None:
        view = _field_map(self.runtime.public_view())
        self.assertEqual(view["retention_days"]["value"], 9)
        self.assertFalse(view["retention_days"]["using_default"])
        self.assertTrue(view["report.idle_avg_lt"]["using_default"])
        self.assertEqual(view["report.idle_avg_lt"]["value"], 5.0)
        self.assertIn("影响", view["anomaly.cpu_load_per_core_warn"]["description"])
        self.assertIsNone(view["token"]["value"])
        blob = json.dumps(self.runtime.public_view(), ensure_ascii=False)
        self.assertNotIn("custom_flag", blob)

        code, payload = self.runtime.apply(
            {
                "values": {
                    "anomaly.cpu_load_per_core_warn": 1.5,
                    "report.idle_avg_lt": 8,
                    "report.idle_busy_lt": 0.2,
                }
            }
        )
        self.assertEqual(code, 200, payload)
        self.assertEqual(self.storage.retention_days, 9)
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["custom_flag"])
        self.assertEqual(saved["alerts"]["legacy"], 1)
        self.assertEqual(saved["retention_days"], 9)
        self.assertEqual(saved["anomaly"]["cpu_load_per_core_warn"], 1.5)
        self.assertEqual(saved["report"]["idle_avg_lt"], 8)
        self.assertNotIn("cpu_warn_percent", saved.get("anomaly") or {})

        code, rejected = self.runtime.apply({"values": {"retention_days": -1}})
        self.assertEqual(code, 400, rejected)
        with open(self.path, encoding="utf-8") as handle:
            again = json.load(handle)
        self.assertEqual(again["retention_days"], 9)
        self.assertEqual(self.storage.retention_days, 9)

        code, rejected = self.runtime.apply(
            {"values": {"anomaly.cpu_warn_percent": 99, "anomaly.cpu_critical_percent": 90}}
        )
        self.assertEqual(code, 400, rejected)
        self.assertIn("低于", rejected["error"])

        code, restored = self.runtime.apply({"restore": ["retention_days", "report.idle_avg_lt"]})
        self.assertEqual(code, 200, restored)
        with open(self.path, encoding="utf-8") as handle:
            after = json.load(handle)
        self.assertNotIn("retention_days", after)
        self.assertNotIn("idle_avg_lt", after.get("report") or {})
        self.assertEqual(after["report"]["idle_busy_lt"], 0.2)
        self.assertEqual(self.storage.retention_days, 7)
        self.assertTrue(after["custom_flag"])

    def test_alias_restore_clears_old_key_and_schedule_is_live(self) -> None:
        code, payload = self.runtime.apply(
            {"values": {"anomaly.npu_temp_warn_c": 70}}
        )
        self.assertEqual(code, 400, payload)
        doc = {"anomaly": {"npu_temp_warn_c": 70}}
        self.runtime.user_doc = doc
        self.runtime.effective = merge_center_config(doc)
        view = _field_map(self.runtime.public_view())
        self.assertEqual(view["anomaly.accel_temp_warn_c"]["value"], 70)
        self.assertFalse(view["anomaly.accel_temp_warn_c"]["using_default"])
        code, payload = self.runtime.apply({"restore": ["anomaly.accel_temp_warn_c"]})
        self.assertEqual(code, 200, payload)
        self.assertNotIn("npu_temp_warn_c", self.runtime.user_doc.get("anomaly") or {})
        self.assertEqual(self.runtime.anomaly()["accel_temp_warn_c"], 85)

        code, payload = self.runtime.apply(
            {
                "values": {
                    "report_schedule.daily.hour": 6,
                    "report_schedule.daily.minute": 6,
                    "cleanup_interval_seconds": 120,
                    "offline_seconds": 30,
                }
            }
        )
        self.assertEqual(code, 200, payload)
        self.assertEqual(self.scheduler.schedule["daily"]["hour"], 6)
        self.assertEqual(self.scheduler.schedule["daily"]["minute"], 6)
        self.assertEqual(self.scheduler.schedule["weekly"]["minute"], 20)
        self.assertEqual(self.cleanup.interval, 120)
        self.assertEqual(self.storage.offline_seconds, 30)
        self.assertEqual(
            period_util_thresholds(self.runtime.period_util())["cpu_percent"], 80.0
        )

    def test_restore_all_keeps_unknown_and_port_needs_restart(self) -> None:
        code, payload = self.runtime.apply({"values": {"port": 9090, "token": AGENT}})
        self.assertEqual(code, 200, payload)
        self.assertIn("port", payload["restart_required"])
        self.assertNotIn(AGENT, json.dumps(payload, ensure_ascii=False))
        hint = _field_map(payload["center"])["token"]["hint"]
        self.assertIn(AGENT[-4:], hint)
        self.assertNotIn("agent-secret", hint)
        self.assertEqual(self.runtime.agent_token(), AGENT)
        code, payload = self.runtime.apply({"restore_all": True})
        self.assertEqual(code, 200, payload)
        with open(self.path, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["custom_flag"])
        self.assertNotIn("port", saved)
        self.assertNotIn("token", saved)
        self.assertEqual(self.runtime.agent_token(), "")
        self.assertEqual(self.storage.retention_days, 7)

    def test_retention_zero_keeps_history_and_old_window(self) -> None:
        code, payload = self.runtime.apply({"values": {"retention_days": 0}})
        self.assertEqual(code, 200, payload)
        self.assertEqual(self.storage.retention_days, 0)
        old_ts = int(time.time()) - 10 * 86400
        self.storage.upsert_metric(
            {
                "host_id": "kept",
                "hostname": "kept",
                "host_type": "cpu",
                "timestamp": old_ts,
                "system": {"cpu_percent": 3},
            }
        )
        self.assertEqual(self.storage.cleanup_old_metrics(), 0)
        self.assertEqual(self.storage.history_count("kept"), 1)
        now = int(time.time())
        err, window = self.storage.resolve_period_window(
            from_ts=old_ts, to_ts=now, now=now
        )
        self.assertIsNone(err)
        self.assertEqual(window["retention_days"], 0)
        self.assertFalse(window["clamped"])
        self.assertEqual(window["from_ts"], old_ts)


class SettingsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "monitor.db")
        self.path = os.path.join(self.tmp.name, "center.json")
        self.storage = Storage(self.db, offline_seconds=90, retention_days=7)
        self.admin = AdminSettingsStore(self.db)
        self.scheduler = ReportScheduler(self.storage)
        self.cleanup = CleanupControl(3600)
        self.runtime = CenterRuntime(
            {"token": "old-agent"},
            self.path,
            storage=self.storage,
            scheduler=self.scheduler,
            cleanup=self.cleanup,
        )
        self.httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(
                self.storage,
                "old-agent",
                admin_store=self.admin,
                runtime=self.runtime,
            ),
        )
        self.base = "http://127.0.0.1:%s" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _post_metric(self, host_id, ts, cpu, token="old-agent"):
        return _request(
            "POST",
            self.base + "/api/v1/metrics",
            {
                "host_id": host_id,
                "hostname": host_id,
                "host_type": "cpu",
                "timestamp": ts,
                "token": token,
                "system": {
                    "cpu_percent": cpu,
                    "cpu_count": 4,
                    "mem_percent": 10,
                    "disk_percent": 10,
                    "load1": 0.2,
                    "primary_ip": "10.9.9.9",
                },
            },
        )

    def test_open_write_then_token_gate_and_live_behavior(self) -> None:
        code, page = _request("GET", self.base + "/api/v1/settings")
        self.assertEqual(code, 200, page)
        groups = page["center"]["groups"]
        self.assertEqual(groups[0]["id"], "retention")
        self.assertEqual(groups[0]["fields"][0]["key"], "retention_days")
        self.assertTrue(groups[0]["fields"][0]["using_default"])
        self.assertIn("影响", groups[0]["fields"][0]["description"])
        self.assertNotIn("old-agent", json.dumps(page, ensure_ascii=False))

        code, saved = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"retention_days": 1, "anomaly.cpu_warn_percent": 40}}},
        )
        self.assertEqual(code, 200, saved)
        self.assertEqual(self.storage.retention_days, 1)
        self.assertNotIn("old-agent", json.dumps(saved, ensure_ascii=False))
        with open(self.path, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        self.assertEqual(on_disk["retention_days"], 1)
        self.assertEqual(on_disk["token"], "old-agent")

        now = int(time.time())
        code, ingested = self._post_metric("box-1", now, 50)
        self.assertEqual(code, 200, ingested)
        code, anomaly = _request("GET", self.base + "/api/v1/anomaly")
        self.assertEqual(code, 200, anomaly)
        self.assertEqual(anomaly["thresholds"]["cpu_warn_percent"], 40)
        host = next(item for item in anomaly["hosts"] if item["host_id"] == "box-1")
        self.assertEqual(host["anomaly"]["overall"], "warn")

        code, relaxed = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"anomaly.cpu_warn_percent": 90}}},
        )
        self.assertEqual(code, 200, relaxed)
        code, anomaly = _request("GET", self.base + "/api/v1/anomaly")
        host = next(item for item in anomaly["hosts"] if item["host_id"] == "box-1")
        self.assertEqual(host["anomaly"]["overall"], "normal")

        old_ts = now - 10 * 86400
        code, old = self._post_metric("old-box", old_ts, 5)
        self.assertEqual(code, 200, old)
        self.assertGreaterEqual(self.storage.cleanup_old_metrics(), 1)
        self.assertEqual(self.storage.history_count("old-box"), 0)
        self.assertGreaterEqual(self.storage.history_count("box-1"), 1)
        self.assertIsNotNone(self.storage.get_host("old-box"))
        self.assertIsNotNone(self.storage.get_host("box-1"))

        code, busy = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"period_util.busy_cpu_percent": 10}}},
        )
        self.assertEqual(code, 200, busy)
        code, stats = _request("GET", self.base + "/api/v1/period-stats?minutes=60")
        self.assertEqual(code, 200, stats)
        self.assertEqual(stats["busy_thresholds"]["cpu_percent"], 10)

        bounds = report_period_bounds("day", now)
        retention_floor = now - int(self.storage.retention_days) * 86400
        sample_ts = max(int(bounds["from_ts"]), retention_floor + 30)
        if sample_ts >= int(bounds["to_ts"]):
            sample_ts = int(bounds["to_ts"]) - 1
        self.assertGreaterEqual(sample_ts, retention_floor)
        self.assertLess(sample_ts, int(bounds["to_ts"]))
        code, window_point = self._post_metric("idle-box", sample_ts, 14)
        self.assertEqual(code, 200, window_point)
        code, ping = self._post_metric("idle-box", now, 14)
        self.assertEqual(code, 200, ping)
        code, calm = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"period_util.busy_cpu_percent": 80}}},
        )
        self.assertEqual(code, 200, calm)
        code, rules = _request(
            "POST",
            self.base + "/api/v1/settings",
            {
                "center": {
                    "values": {
                        "report.min_samples": 1,
                        "report.idle_avg_lt": 20,
                        "report.idle_busy_lt": 0.5,
                        "report.high_busy_gte": 0.99,
                        "report.high_p95_gte": 99,
                    }
                }
            },
        )
        self.assertEqual(code, 200, rules)
        code, generated = _request(
            "POST",
            self.base + "/api/v1/reports/generate",
            {"period": "day", "as_of": now},
        )
        self.assertEqual(code, 200, generated)
        payload = generated["report"]["payload"]
        self.assertEqual(payload["rules"]["idle_avg_lt"], 20)
        self.assertEqual(payload["rules"]["min_samples"], 1)
        self.assertIn("平均 <20%", generated["report"]["markdown"])
        poles = {row["host_id"]: row["pole"] for row in payload["rankings"]}
        self.assertEqual(poles["idle-box"], "idle")

        code, scheduled = _request(
            "POST",
            self.base + "/api/v1/settings",
            {
                "center": {
                    "values": {
                        "report_schedule.daily.hour": 6,
                        "report_schedule.daily.minute": 6,
                    }
                }
            },
        )
        self.assertEqual(code, 200, scheduled)
        self.assertEqual(self.scheduler.schedule["daily"]["hour"], 6)
        self.assertEqual(self.scheduler.report_rules["idle_avg_lt"], 20)

        code, secret = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"token": AGENT}}},
        )
        self.assertEqual(code, 200, secret)
        self.assertNotIn(AGENT, json.dumps(secret, ensure_ascii=False))
        self.assertIn(AGENT[-4:], secret["center"]["groups"][-2]["fields"][0]["hint"])
        code, denied = self._post_metric("box-2", now, 3, token="old-agent")
        self.assertEqual(code, 401, denied)
        code, accepted = self._post_metric("box-2", now, 3, token=AGENT)
        self.assertEqual(code, 200, accepted)
        code, not_mgmt = self._post_metric("box-3", now, 3, token=MGMT)
        self.assertEqual(code, 401, not_mgmt)

        code, port = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"port": 9090}}},
        )
        self.assertEqual(code, 200, port)
        self.assertIn("port", port["restart_required"])
        code, still = _request("GET", self.base + "/api/v1/health")
        self.assertEqual(code, 200, still)

        code, opened = _request(
            "POST",
            self.base + "/api/v1/settings",
            {
                "management_token": MGMT,
                "push_enabled": True,
                "push_channel": "wecom",
                "push_webhook_url": WEBHOOK,
                "center": {"values": {"offline_seconds": 45}},
            },
        )
        self.assertEqual(code, 200, opened)
        dumped = json.dumps(opened, ensure_ascii=False)
        self.assertNotIn(MGMT, dumped)
        self.assertNotIn(AGENT, dumped)
        self.assertNotIn("secret-key-abcdef", dumped)
        self.assertIn("cdef", opened["settings"]["push_webhook_hint"])
        self.assertEqual(self.storage.offline_seconds, 45)

        code, blocked = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"values": {"retention_days": 3}}},
        )
        self.assertEqual(code, 401, blocked)
        self.assertEqual(blocked["code"], "management_token_required")
        self.assertEqual(self.storage.retention_days, 1)
        code, reads = _request("GET", self.base + "/api/v1/settings")
        self.assertEqual(code, 200, reads)
        code, hosts = _request("GET", self.base + "/api/v1/hosts")
        self.assertEqual(code, 200, hosts)

        auth = {"Content-Type": "application/json", "X-Management-Token": MGMT}
        code, allowed = _request(
            "POST",
            self.base + "/api/v1/settings",
            {"center": {"restore": ["retention_days"]}},
            headers=auth,
        )
        self.assertEqual(code, 200, allowed)
        self.assertEqual(self.storage.retention_days, 7)
        self.assertNotIn(MGMT, json.dumps(allowed, ensure_ascii=False))
        self.assertNotIn(AGENT, json.dumps(allowed, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
