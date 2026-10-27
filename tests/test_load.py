"""负载读取失败不用 0，load5 与 load1 共用每核阈值。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from typing import Dict, List, Optional
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics import system as system_metrics
from center import api as api_mod
from center.anomaly import judge_host_payload
from center.storage import Storage


def _system(**extra: object) -> dict:
    base = {
        "cpu_percent": 10,
        "cpu_count": 8,
        "mem_percent": 10,
        "disk_percent": 10,
    }
    base.update(extra)
    return base


class SampleLoadTests(unittest.TestCase):
    def test_os_zero_is_kept(self) -> None:
        with mock.patch("agent.metrics.system.os.getloadavg", return_value=(0.0, 0.0, 0.0)):
            self.assertEqual(
                system_metrics.sample_load(),
                {"load1": 0.0, "load5": 0.0, "load15": 0.0},
            )

    def test_both_reads_fail_are_null(self) -> None:
        with mock.patch("agent.metrics.system.os.getloadavg", side_effect=OSError("no")):
            with mock.patch("agent.metrics.system._read_text", return_value=None):
                self.assertEqual(
                    system_metrics.sample_load(),
                    {"load1": None, "load5": None, "load15": None},
                )
                self.assertIsNone(system_metrics.sample_load1())

    def test_proc_keeps_real_zero_and_leaves_missing_field_null(self) -> None:
        with mock.patch("agent.metrics.system.os.getloadavg", side_effect=OSError("no")):
            with mock.patch("agent.metrics.system._read_text", return_value="0.00 1.50\n"):
                got = system_metrics.sample_load()
        self.assertEqual(got["load1"], 0.0)
        self.assertEqual(got["load5"], 1.5)
        self.assertIsNone(got["load15"])

    def test_proc_bad_token_does_not_zero_the_rest(self) -> None:
        with mock.patch("agent.metrics.system.os.getloadavg", side_effect=AttributeError("missing")):
            with mock.patch("agent.metrics.system._read_text", return_value="1.25 nope 0.50"):
                got = system_metrics.sample_load()
        self.assertEqual(got["load1"], 1.25)
        self.assertIsNone(got["load5"])
        self.assertEqual(got["load15"], 0.5)

    def test_proc_garbage_is_null(self) -> None:
        with mock.patch("agent.metrics.system.os.getloadavg", side_effect=OSError("no")):
            with mock.patch("agent.metrics.system._read_text", return_value="not-a-load"):
                self.assertEqual(
                    system_metrics.sample_load(),
                    {"load1": None, "load5": None, "load15": None},
                )


class LoadAnomalyTests(unittest.TestCase):
    def _codes(
        self, system: Dict[str, object], thresholds: Optional[Dict[str, float]] = None
    ) -> List[dict]:
        judged = judge_host_payload(
            {"system": system},
            thresholds=thresholds,
            online=True,
        )
        return [item for item in judged["findings"] if item["code"] == "cpu_load_high"]

    def test_null_load_is_skipped(self) -> None:
        findings = self._codes(_system(load1=None, load5=None))
        self.assertEqual(findings, [])

    def test_real_zero_is_not_high(self) -> None:
        findings = self._codes(_system(load1=0, load5=0.0))
        self.assertEqual(findings, [])

    def test_load5_uses_same_threshold(self) -> None:
        findings = self._codes(_system(load1=1.0, load5=16.0))
        self.assertEqual(len(findings), 1)
        self.assertIn("load5=", findings[0]["message"])
        self.assertNotIn("load1=", findings[0]["message"])
        self.assertEqual(findings[0]["level"], "warn")
        judged = judge_host_payload({"system": _system(load1=1.0, load5=16.0)}, online=True)
        self.assertEqual(judged["cpu"]["status"], "warn")
        self.assertEqual(judged["overall"], "warn")

    def test_load1_still_warns(self) -> None:
        findings = self._codes(_system(load1=16.0, load5=1.0))
        self.assertEqual(len(findings), 1)
        self.assertIn("load1=", findings[0]["message"])

    def test_both_windows_warn(self) -> None:
        findings = self._codes(_system(load1=20.0, load5=18.0))
        self.assertEqual(len(findings), 2)
        messages = " ".join(item["message"] for item in findings)
        self.assertIn("load1=", messages)
        self.assertIn("load5=", messages)

    def test_custom_threshold_applies_to_load5(self) -> None:
        findings = self._codes(
            _system(load1=1.0, load5=20.0),
            thresholds={"cpu_load_per_core_warn": 4.0},
        )
        self.assertEqual(findings, [])


class LoadSummaryTests(unittest.TestCase):
    def test_failed_load_stays_null_on_host_list(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        storage = Storage(os.path.join(tmp.name, "t.db"))
        try:
            payload = {
                "host_id": "h-null",
                "hostname": "h-null",
                "host_type": "cpu",
                "timestamp": int(time.time()),
                "system": {
                    "cpu_percent": 10,
                    "cpu_count": 8,
                    "mem_percent": 10,
                    "disk_percent": 10,
                    "load1": None,
                    "load5": None,
                    "load15": None,
                },
            }
            code, resp = api_mod.handle_metrics_post(
                storage, json.dumps(payload).encode("utf-8")
            )
            self.assertEqual(code, 200)
            self.assertTrue(resp["ok"])
            _, hosts = api_mod.handle_hosts_list(storage)
            host = hosts["hosts"][0]
            self.assertIsNone(host["load1"])
            self.assertIsNone(host["load5"])
            codes = [item["code"] for item in host["anomaly"]["findings"]]
            self.assertNotIn("cpu_load_high", codes)
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
