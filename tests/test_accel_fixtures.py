"""真实加速卡样例（序列号 / UUID 已替换）和标明 synthetic 的边界输入。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.accelerators import (  # noqa: E402
    _parse_cnmon_table,
    _read_cnmon_json,
    collect_accelerators,
    parse_cnmon_json,
)
from agent.metrics.amd import parse_amd_smi_json, parse_rocm_smi_csv, parse_rocm_smi_json, parse_rocm_smi_text
from agent.metrics.gpu import parse_nvidia_smi_csv
from agent.metrics.npu import (
    _parse_info_table,
    _parse_chip_mapping,
    collect_npus,
    output_is_npu_error,
)
from agent.metrics.probe import (  # noqa: E402
    merge_tool_and_pci,
    norm_bdf,
    parse_nvidia_tool_version,
    run_cmd,
)

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "accel")

# 来自各主机 nvidia_query_agent_powerlimit.txt 的第一张卡。
NVIDIA_FIRST = {
    "192.168.15.96": {
        "count": 8,
        "name": "NVIDIA GeForce RTX 4090",
        "help_version": "580.173.02",
        "version_file_ok": True,
        "util_percent": 0.0,
        "mem_total_mb": 24564.0,
        "mem_used_mb": 898.0,
        "temp_c": 35.0,
        "power_w": 25.07,
        "power_limit_w": 450.0,
        "fan_percent": 31.0,
    },
    "192.168.198.101": {
        "count": 2,
        "name": "NVIDIA GeForce RTX 3090",
        "help_version": "535.309.01",
        "version_file_ok": False,
        "version_error": "not recognized",
        "util_percent": 0.0,
        "mem_total_mb": 24576.0,
        "mem_used_mb": 10.0,
        "temp_c": 42.0,
        "power_w": 25.59,
        "power_limit_w": 350.0,
        "fan_percent": 31.0,
    },
    "192.168.198.63": {
        "count": 1,
        "name": "Quadro P4000",
        "help_version": "470.256.02",
        "version_file_ok": False,
        "version_error": "Invalid combination",
        "util_percent": 0.0,
        "mem_total_mb": 8117.0,
        "mem_used_mb": 32.0,
        "temp_c": 38.0,
        "power_w": 5.93,
        "power_limit_w": 105.0,
        "fan_percent": 46.0,
    },
    "192.168.198.102": {
        "count": 2,
        "name": "NVIDIA GeForce RTX 3090",
        "help_version": "595.99.02",
        "version_file_ok": True,
        "util_percent": 0.0,
        "mem_total_mb": 24576.0,
        "mem_used_mb": 279.0,
        "temp_c": 40.0,
        "power_w": 32.43,
        "power_limit_w": 350.0,
        "fan_percent": 30.0,
    },
    "192.168.198.106": {
        "count": 2,
        "name": "NVIDIA GeForce RTX 3090",
        "help_version": "595.99.02",
        "version_file_ok": True,
        "util_percent": 0.0,
        "mem_total_mb": 24576.0,
        "mem_used_mb": 15.0,
        "temp_c": 38.0,
        "power_w": 11.13,
        "power_limit_w": 350.0,
        "fan_percent": 30.0,
    },
}

CNMON_JSON = {
    "172.24.5.49": {
        "name": "MLU370-S4",
        "util_percent": 15.0,
        "temp_c": 74.0,
        "mem_used_mb": 445.0,
        "mem_total_mb": 23365.0,
        "power_w": 42.0,
        "health": "Good",
        "bus_id": "0000:86:00.0",
        "tool_version": "v5.10.13",
    },
    "192.168.198.108": {
        "name": "MLU370-S4",
        "util_percent": 0.0,
        "temp_c": 73.0,
        "mem_used_mb": 0.0,
        "mem_total_mb": 23365.0,
        "power_w": 28.0,
        "health": "Good",
        "bus_id": "0000:01:00.0",
        "tool_version": "v5.10.13",
    },
    "192.168.198.107": {
        "name": "MLU270-F4",
        "util_percent": 0.0,
        "temp_c": 46.0,
        "mem_used_mb": 4021.0,
        "mem_total_mb": 16384.0,
        "power_w": 23.0,
        "health": None,
        "bus_id": "0000:02:00.0",
        "tool_version": "1.19.5",
    },
}

CNMON_TABLE = {
    "172.24.5.49": {
        "name": "MLU370-S4",
        "util_percent": 18.0,
        "temp_c": 74.0,
        "power_w": 45.0,
        "mem_used_mb": 445.0,
        "mem_total_mb": 23365.0,
        "bus_id": "0000:86:00.0",
    },
    "192.168.198.108": {
        "name": "MLU370-S4",
        "util_percent": 0.0,
        "temp_c": 73.0,
        "power_w": 28.0,
        "mem_used_mb": 0.0,
        "mem_total_mb": 23365.0,
        "bus_id": "0000:01:00.0",
    },
    "192.168.198.107": {
        "name": "MLU270-F4",
        "util_percent": 0.0,
        "temp_c": 46.0,
        "power_w": 23.0,
        "mem_used_mb": 4021.0,
        "mem_total_mb": 16384.0,
        "bus_id": None,
    },
}


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _nvidia(ip: str, name: str) -> str:
    return _read(os.path.join(FIXTURE, "nvidia", ip, "raw", name))


class RedactionTests(unittest.TestCase):
    def test_committed_fixtures_use_placeholder_ids(self):
        leaked = []
        gpu_real = re.compile(
            r"GPU-[0-9a-fA-F]{8}-(?!0000-4000-8000)[0-9a-fA-F]{4}-"
        )
        for dirpath, _dirs, files in os.walk(FIXTURE):
            for name in files:
                path = os.path.join(dirpath, name)
                text = _read(path)
                if gpu_real.search(text):
                    leaked.append(path)
                for needle in ("532412300274", "182005300127", "C6AF1992", "22efab5b"):
                    if needle in text:
                        leaked.append(path + ":" + needle)
        self.assertEqual(leaked, [])


class NvidiaFixtureTests(unittest.TestCase):
    def test_each_driver_query(self):
        for ip, expect in NVIDIA_FIRST.items():
            text = _nvidia(ip, "nvidia_query_agent_powerlimit.txt")
            cards = parse_nvidia_smi_csv(text, has_power_limit=True)
            self.assertEqual(len(cards), expect["count"], ip)
            card = cards[0]
            self.assertEqual(card["name"], expect["name"], ip)
            self.assertTrue(card["uuid"].startswith("GPU-00000000-0000-4000-8000-"), ip)
            for key in (
                "util_percent",
                "mem_total_mb",
                "mem_used_mb",
                "temp_c",
                "power_w",
                "power_limit_w",
                "fan_percent",
            ):
                self.assertEqual(card[key], expect[key], "%s %s" % (ip, key))
            help_text = _nvidia(ip, "nvidia_help_head.txt")
            version_text = _nvidia(ip, "nvidia_version.txt")
            self.assertEqual(parse_nvidia_tool_version(help_text), expect["help_version"], ip)
            if expect["version_file_ok"]:
                self.assertEqual(
                    parse_nvidia_tool_version("", version_text), expect["help_version"], ip
                )
            else:
                self.assertIn(expect["version_error"], version_text)
                self.assertIsNone(parse_nvidia_tool_version("", version_text))

    def test_eight_4090s_span_two_pci_domains(self):
        text = _nvidia("192.168.15.96", "nvidia_query_user.txt")
        buses = [
            norm_bdf(item)
            for item in re.findall(r"[0-9a-fA-F]{4,8}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]", text)
        ]
        self.assertEqual(len(buses), 8)
        self.assertEqual(len(set(buses)), 8)
        self.assertEqual(set(bus.split(":")[0] for bus in buses), {"0000", "0001"})

    def test_version_flag_error_is_not_a_card(self):
        text = _nvidia("192.168.198.101", "nvidia_version.txt")
        self.assertEqual(parse_nvidia_smi_csv(text), [])

    def test_not_supported_field_keeps_the_card(self):
        # synthetic：某一列不支持时只空出该字段
        text = "0, GPU-00000000-0000-4000-8000-000000000099, Synthetic GPU, [Not Supported], 1024, N/A, 40, 12.5, [N/A], [Not Supported]"
        cards = parse_nvidia_smi_csv(text, has_power_limit=True)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["name"], "Synthetic GPU")
        self.assertIsNone(cards[0]["util_percent"])
        self.assertIsNone(cards[0]["mem_used_mb"])
        self.assertEqual(cards[0]["temp_c"], 40.0)
        self.assertEqual(cards[0]["power_w"], 12.5)
        self.assertIsNone(cards[0]["fan_percent"])


class NvidiaDriverlessTests(unittest.TestCase):
    def test_command_not_found_and_two_pci_cards(self):
        err = _read(
            os.path.join(
                FIXTURE,
                "nvidia-driverless",
                "192.168.198.109",
                "raw",
                "nvidia_query_agent.txt",
            )
        )
        self.assertIn("command not found", err)
        self.assertEqual(parse_nvidia_smi_csv(err), [])
        result = run_cmd(["/bin/sh", "-c", "printf '%s\\n' 'bash: line 1: nvidia-smi: command not found'; exit 127"])
        self.assertIn("command not found", result.error)
        self.assertEqual(result.returncode, 127)

        root = tempfile.mkdtemp(prefix="nv-pci-")
        self.addCleanup(shutil.rmtree, root, True)
        for bdf in ("0000:5e:00.0", "0000:d8:00.0"):
            slot = os.path.join(root, bdf)
            os.makedirs(slot)
            for name, value in (
                ("vendor", "0x10de\n"),
                ("device", "0x2204\n"),
                ("class", "0x030000\n"),
                ("revision", "0xa1\n"),
            ):
                with open(os.path.join(slot, name), "w", encoding="utf-8") as fh:
                    fh.write(value)

        real_exists = os.path.exists

        def exists(path):
            text = str(path)
            if text.startswith("/dev/nvidia") or text.startswith("/dev/cambricon") or text.startswith("/dev/davinci") or text.startswith("/dev/kfd"):
                return False
            return real_exists(path)

        def which(_name):
            return None

        with mock.patch.dict(os.environ, {"MONITOR_SYSFS_PCI": root}):
            with mock.patch("agent.metrics.gpu.shutil.which", side_effect=which):
                with mock.patch("agent.metrics.amd.shutil.which", side_effect=which):
                    with mock.patch("agent.metrics.npu.shutil.which", side_effect=which):
                        with mock.patch("agent.metrics.accelerators.shutil.which", side_effect=which):
                            with mock.patch("agent.metrics.accelerators.os.path.exists", side_effect=exists):
                                with mock.patch(
                                    "agent.metrics.accelerators._from_rockchip_rknn", return_value=[]
                                ):
                                    cards = collect_accelerators()
        nvidia = [card for card in cards if card.get("vendor") == "nvidia"]
        self.assertEqual(len(nvidia), 2)
        self.assertEqual({card["name"] for card in nvidia}, {"GeForce RTX 3090"})
        self.assertEqual({norm_bdf(card["bus_id"]) for card in nvidia}, {"0000:5e:00.0", "0000:d8:00.0"})
        for card in nvidia:
            self.assertIsNone(card["util_percent"])
            self.assertEqual(card["health"], "abnormal")
            self.assertIn("驱动/管理工具缺失", card["note"])
            self.assertIn("不在 PATH", card["note"])
            self.assertEqual(card["source"], "pci")


class AmdFixtureTests(unittest.TestCase):
    def setUp(self):
        self.raw = os.path.join(FIXTURE, "amd", "172.24.26.50", "raw")

    def test_rocm_json_five_cards_including_negative_subsystem(self):
        text = _read(os.path.join(self.raw, "rocm_agent_json.txt"))
        self.assertIn("-0x1bc7", text)
        cards = parse_rocm_smi_json(text)
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[0]["name"], "AMD Radeon RX 6800")
        self.assertEqual(cards[0]["util_percent"], 0.0)
        self.assertEqual(cards[0]["temp_c"], 47.0)
        self.assertEqual(cards[0]["power_w"], 9.0)
        self.assertEqual(cards[0]["mem_total_mb"], 16368.0)
        self.assertEqual(cards[4]["name"], "AMD Radeon RX 6800")
        self.assertEqual(cards[4]["index"], 4)

    def test_rocm_csv_and_text(self):
        csv_cards = parse_rocm_smi_csv(_read(os.path.join(self.raw, "rocm_agent_csv.txt")))
        self.assertEqual(len(csv_cards), 5)
        self.assertEqual(csv_cards[0]["temp_c"], 47.0)
        text_cards = parse_rocm_smi_text(_read(os.path.join(self.raw, "rocm_agent_text.txt")))
        self.assertEqual(len(text_cards), 5)
        self.assertEqual(text_cards[0]["temp_c"], 48.0)
        self.assertEqual(text_cards[0]["util_percent"], 0.0)

    def test_amd_smi_zero_util_is_kept_and_bus_id_present(self):
        cards = parse_amd_smi_json(
            _read(os.path.join(self.raw, "amdsmi_metric_json.txt")),
            _read(os.path.join(self.raw, "amdsmi_static_json.txt")),
        )
        self.assertEqual(len(cards), 5)
        self.assertEqual(cards[0]["util_percent"], 0.0)
        self.assertEqual(cards[0]["temp_c"], 48.0)
        self.assertEqual(cards[0]["power_w"], 21.0)
        self.assertEqual(cards[0]["mem_used_mb"], 9165.0)
        self.assertEqual(cards[0]["mem_total_mb"], 16368.0)
        self.assertEqual(norm_bdf(cards[0]["bus_id"]), "0000:03:00.0")
        self.assertEqual(cards[3]["util_percent"], 40.0)
        self.assertEqual(cards[3]["name"], "AMD Radeon RX 6800")
        self.assertNotIn("0xC6AF", json.dumps(cards))


class AscendFixtureTests(unittest.TestCase):
    def setUp(self):
        self.raw = os.path.join(FIXTURE, "ascend", "192.168.15.95", "raw")

    def test_info_table_uses_npu_id_1_and_ignores_na_power(self):
        cards = _parse_info_table(_read(os.path.join(self.raw, "npu_info.txt")))
        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card["index"], 1)
        self.assertEqual(card["name"], "310P3")
        self.assertEqual(card["health"], "OK")
        self.assertEqual(card["util_percent"], 5.0)
        self.assertEqual(card["temp_c"], 61.0)
        self.assertIsNone(card["power_w"])
        self.assertEqual(card["mem_used_mb"], 2702.0)
        self.assertEqual(card["mem_total_mb"], 21527.0)
        self.assertEqual(card["mem_percent"], 12.55)
        self.assertEqual(card["bus_id"], "0000:01:00.0")
        chips = _parse_chip_mapping(_read(os.path.join(self.raw, "npu_info_m.txt")))
        self.assertEqual(len(chips), 2)
        self.assertFalse(chips[0]["is_mcu"])
        self.assertTrue(chips[1]["is_mcu"])

    def test_error_outputs_are_not_cards(self):
        invalid = _read(os.path.join(self.raw, "npu_info_t_temp_i0.txt"))
        common = _read(os.path.join(self.raw, "npu_info_t_common_i1_c0.txt"))
        power = _read(os.path.join(self.raw, "npu_info_t_power_i1_c0.txt"))
        self.assertIn("Invalid card id", invalid)
        self.assertIn("Error parameter", common)
        self.assertIn("does not support", power)
        for text in (invalid, common, power):
            self.assertTrue(output_is_npu_error(text))
            self.assertEqual(_parse_info_table(text), [])

    def test_collect_queries_npu_id_1(self):
        calls = []

        def fake(args, timeout=10.0):
            calls.append(list(args))
            if args == ["info"]:
                return _read(os.path.join(self.raw, "npu_info.txt"))
            if args[:2] == ["info", "-m"]:
                return _read(os.path.join(self.raw, "npu_info_m.txt"))
            if "-t" in args and "temp" in args:
                return _read(os.path.join(self.raw, "npu_info_t_temp_i1_c0.txt"))
            if "-t" in args and "usages" in args:
                return _read(os.path.join(self.raw, "npu_info_t_usages_i1_c0.txt"))
            return None

        with mock.patch("agent.metrics.npu._run_npu_smi", side_effect=fake):
            cards = collect_npus()
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["index"], 1)
        self.assertEqual(cards[0]["temp_c"], 61.0)
        self.assertIsNone(cards[0]["power_w"])
        queried = []
        for args in calls:
            if "-i" in args:
                queried.append(args[args.index("-i") + 1])
        self.assertTrue(queried)
        self.assertEqual(set(queried), {"1"})


class CambriconJsonTests(unittest.TestCase):
    def test_json_fixtures(self):
        for ip, expect in CNMON_JSON.items():
            text = _read(os.path.join(FIXTURE, "cambricon", ip, "cnmon_info.json"))
            cards = parse_cnmon_json(text)
            self.assertEqual(len(cards), 1, ip)
            card = cards[0]
            for key, value in expect.items():
                self.assertEqual(card.get(key), value, "%s %s" % (ip, key))
            self.assertEqual(card["source"], "cnmon-json")
            self.assertNotIn("532412300274", text)

    def test_summary_table(self):
        for ip, expect in CNMON_TABLE.items():
            text = _read(os.path.join(FIXTURE, "cambricon", ip, "raw", "cnmon.txt"))
            cards = _parse_cnmon_table(text)
            self.assertEqual(len(cards), 1, ip)
            card = cards[0]
            for key, value in expect.items():
                self.assertEqual(card.get(key), value, "%s %s" % (ip, key))
            self.assertIsNone(card["health"])

    def test_json_flag_uses_temp_dir_and_removes_it(self):
        payload = _read(os.path.join(FIXTURE, "cambricon", "192.168.198.107", "cnmon_info.json"))
        seen = {}

        def fake_run(cmd, timeout=8.0, cwd=None):
            self.assertIsNotNone(cwd)
            self.assertNotEqual(os.path.abspath(cwd), os.path.abspath(ROOT))
            self.assertTrue(os.path.isdir(cwd))
            seen["cwd"] = cwd
            with open(os.path.join(cwd, "cnmon_info.json"), "w", encoding="utf-8") as fh:
                fh.write(payload)
            return payload

        with mock.patch("agent.metrics.accelerators._run", side_effect=fake_run):
            text = _read_cnmon_json(["cnmon"], 5.0)
        self.assertIn("MLU270-F4", text)
        self.assertFalse(os.path.exists(seen["cwd"]))
        self.assertFalse(os.path.exists(os.path.join(ROOT, "cnmon_info.json")))
        cards = parse_cnmon_json(text)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]["health"], None)


class MergeTests(unittest.TestCase):
    def test_bus_id_collapses_tool_and_pci_but_keeps_unmatched(self):
        # synthetic：两张工具卡里只有一张能对上 PCI
        tool = [
            {
                "vendor": "nvidia",
                "vendor_label": "英伟达",
                "name": "Synthetic A",
                "index": 0,
                "bus_id": "00000000:17:00.0",
                "util_percent": 3,
                "health": "OK",
            },
            {
                "vendor": "nvidia",
                "vendor_label": "英伟达",
                "name": "Synthetic B",
                "index": 1,
                "bus_id": "00000001:17:00.0",
                "util_percent": 4,
            },
        ]
        pci = [
            {
                "vendor": "nvidia",
                "name": "PCI A",
                "bdf": "0000:17:00.0",
                "bus_id": "0000:17:00.0",
                "source": "pci",
                "health": "abnormal",
                "util_percent": None,
            },
            {
                "vendor": "nvidia",
                "name": "PCI extra",
                "bdf": "0000:99:00.0",
                "bus_id": "0000:99:00.0",
                "source": "pci",
                "health": "abnormal",
                "util_percent": None,
                "note": "驱动/管理工具缺失",
            },
        ]
        cards = merge_tool_and_pci(tool, pci, {})
        names = [card["name"] for card in cards]
        self.assertEqual(names, ["Synthetic A", "Synthetic B", "PCI extra"])
        self.assertEqual(cards[0]["bus_id"], "0000:17:00.0")
        self.assertEqual(cards[1]["bus_id"], "0001:17:00.0")

    def test_tool_failure_is_attached_and_missing_binary_does_not_invent_a_card(self):
        pci = [
            {
                "vendor": "nvidia",
                "name": "GeForce RTX 3090",
                "bus_id": "0000:5e:00.0",
                "source": "pci",
                "health": "abnormal",
                "note": "驱动/管理工具缺失",
                "util_percent": None,
            }
        ]
        cards = merge_tool_and_pci([], pci, {"nvidia": "nvidia-smi exit 127: command not found"})
        self.assertEqual(len(cards), 1)
        self.assertIn("command not found", cards[0]["note"])
        self.assertIn("驱动/管理工具缺失", cards[0]["note"])
        self.assertEqual(merge_tool_and_pci([], [], {"nvidia": "nvidia-smi 不在 PATH"}), [])
        failed = merge_tool_and_pci([], [], {"cambricon": "cnmon info exit 1: driver mismatch"})
        self.assertEqual(len(failed), 1)
        self.assertIn("driver mismatch", failed[0]["note"])
        self.assertIsNone(failed[0]["util_percent"])


if __name__ == "__main__":
    unittest.main()
