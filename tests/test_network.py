#!/usr/bin/env python3
"""网络额定链路速率解析与汇聚。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from agent.metrics.network import (
    aggregate_rated,
    iface_is_virtual,
    list_iface_links,
    net_util_percent,
    parse_ethtool_speed,
    parse_proc_net_dev,
    parse_sysfs_speed,
    select_rated_ifaces,
)


PROC_NET_DEV = """\
Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 1000     10    0    0    0     0          0         0   1000     10    0    0    0     0       0          0
  eth0: 500000   100    0    0    0     0          0         0  200000    80    0    0    0     0       0          0
  eth1: 100      2     0    0    0     0          0         0      50     1    0    0    0     0       0          0
docker0: 9999    1     0    0    0     0          0         0    9999     1    0    0    0     0       0          0
"""


def _write_iface(root: str, name: str, oper: str, speed: str = None, master: bool = False) -> None:
    path = os.path.join(root, name)
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "operstate"), "w", encoding="utf-8") as f:
        f.write(oper + "\n")
    if speed is not None:
        with open(os.path.join(path, "speed"), "w", encoding="utf-8") as f:
            f.write(speed + "\n")
    if master:
        os.symlink("../bond0", os.path.join(path, "master"))


class NetworkRatedTests(unittest.TestCase):
    def test_parse_proc_net_dev(self) -> None:
        c = parse_proc_net_dev(PROC_NET_DEV)
        self.assertEqual(c["lo"], (1000, 1000))
        self.assertEqual(c["eth0"], (500000, 200000))
        self.assertEqual(c["eth1"], (100, 50))
        self.assertIn("docker0", c)

    def test_virtual_ifaces(self) -> None:
        self.assertTrue(iface_is_virtual("lo"))
        self.assertTrue(iface_is_virtual("docker0"))
        self.assertTrue(iface_is_virtual("veth1a2b"))
        self.assertTrue(iface_is_virtual("br-abc"))
        self.assertFalse(iface_is_virtual("eth0"))
        self.assertFalse(iface_is_virtual("ens192"))
        self.assertFalse(iface_is_virtual("bond0"))

    def test_parse_sysfs_speed(self) -> None:
        self.assertEqual(parse_sysfs_speed("1000\n"), 1000)
        self.assertEqual(parse_sysfs_speed("10000"), 10000)
        self.assertIsNone(parse_sysfs_speed("-1"))
        self.assertIsNone(parse_sysfs_speed("Unknown!\n"))
        self.assertIsNone(parse_sysfs_speed(""))
        self.assertIsNone(parse_sysfs_speed(None))

    def test_parse_ethtool_speed(self) -> None:
        self.assertEqual(parse_ethtool_speed("Speed: 1000Mb/s"), 1000)
        self.assertEqual(parse_ethtool_speed("\tSpeed: 10Gb/s\nDuplex: Full"), 10000)
        self.assertEqual(parse_ethtool_speed("Speed: 2.5Gb/s"), 2500)
        self.assertIsNone(parse_ethtool_speed("Speed: Unknown!"))
        self.assertIsNone(parse_ethtool_speed(""))

    def test_util_percent(self) -> None:
        self.assertEqual(net_util_percent(50, 1000), 5.0)
        self.assertEqual(net_util_percent(0, 1000), 0.0)
        self.assertEqual(net_util_percent(2000, 1000), 100.0)
        self.assertIsNone(net_util_percent(10, 0))
        self.assertIsNone(net_util_percent(10, None))
        self.assertIsNone(net_util_percent(None, 1000))

    def test_sum_up_physical_skip_virtual(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_iface(tmp, "eth0", "up", "1000")
            _write_iface(tmp, "eth1", "up", "10000")
            _write_iface(tmp, "eth2", "down", "1000")
            _write_iface(tmp, "docker0", "up", "10000")
            _write_iface(tmp, "lo", "unknown", "-1")
            ifaces = list_iface_links(tmp)
            rated = aggregate_rated(ifaces)
            self.assertEqual(rated["net_rated_mbps"], 11000)
            self.assertEqual(rated["net_link_mbps"], 10000)
            names = {i["name"] for i in rated["net_ifaces"]}
            self.assertIn("eth0", names)
            self.assertIn("eth1", names)
            self.assertNotIn("docker0", names)
            self.assertNotIn("lo", names)

    def test_bond_skips_enslaved_slaves(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_iface(tmp, "bond0", "up", "20000")
            _write_iface(tmp, "eth0", "up", "10000", master=True)
            _write_iface(tmp, "eth1", "up", "10000", master=True)
            rated = aggregate_rated(list_iface_links(tmp))
            self.assertEqual(rated["net_rated_mbps"], 20000)
            names = {i["name"] for i in rated["net_ifaces"]}
            self.assertEqual(names, {"bond0"})

    def test_ethtool_fallback_when_sysfs_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_iface(tmp, "eth0", "up")  # no speed file

            def ethtool(name: str):
                return 25000 if name == "eth0" else None

            ifaces = list_iface_links(tmp, ethtool_fn=ethtool)
            self.assertEqual(ifaces[0]["speed_mbps"], 25000)
            self.assertEqual(aggregate_rated(ifaces)["net_rated_mbps"], 25000)

    def test_container_fallback_keeps_eth0(self) -> None:
        """无物理/bond 时回退到非 lo 接口（容器 eth0）。"""
        ifaces = [
            {
                "name": "eth0",
                "up": True,
                "speed_mbps": 10000,
                "virtual": False,
                "enslaved": False,
            }
        ]
        selected = select_rated_ifaces(ifaces)
        self.assertEqual(len(selected), 1)
        self.assertEqual(aggregate_rated(ifaces)["net_rated_mbps"], 10000)


if __name__ == "__main__":
    unittest.main()
