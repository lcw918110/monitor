#!/usr/bin/env python3
"""演示数据：CPU / NPU 监测与异常判定场景。"""

from __future__ import annotations

import argparse
import json
import time
import urllib.request


def post(url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def main() -> None:
    parser = argparse.ArgumentParser(description="CPU/NPU 演示数据灌入")
    parser.add_argument("--url", default="http://127.0.0.1:8080/api/v1/metrics")
    parser.add_argument("--points", type=int, default=24)
    args = parser.parse_args()
    now = int(time.time())

    hosts = [
        {
            "host_id": "app-cpu-01",
            "hostname": "app-cpu-01",
            "host_type": "app",
            "base_cpu": 35,
            "cpu_count": 16,
            "npus": [],
        },
        {
            "host_id": "npu-lab-01",
            "hostname": "npu-lab-01",
            "host_type": "npu",
            "base_cpu": 42,
            "cpu_count": 64,
            "npus": [
                {
                    "index": 0,
                    "chip_id": 0,
                    "name": "Ascend 910B",
                    "health": "OK",
                    "util_percent": 70,
                    "mem_used_mb": 22000,
                    "mem_total_mb": 65536,
                    "mem_percent": 33.6,
                    "temp_c": 62,
                    "power_w": 280,
                    "bus_id": "0000:01:00.0",
                },
                {
                    "index": 1,
                    "chip_id": 0,
                    "name": "Ascend 910B",
                    "health": "Warning",
                    "util_percent": 96,
                    "mem_used_mb": 60000,
                    "mem_total_mb": 65536,
                    "mem_percent": 91.6,
                    "temp_c": 91,
                    "power_w": 350,
                    "bus_id": "0000:02:00.0",
                },
            ],
        },
        {
            "host_id": "npu-offline-01",
            "hostname": "npu-offline-01",
            "host_type": "npu",
            "base_cpu": 10,
            "cpu_count": 32,
            "npus": [
                {
                    "index": 0,
                    "name": "Ascend 310",
                    "health": "OK",
                    "util_percent": 5,
                    "mem_used_mb": 500,
                    "mem_total_mb": 8192,
                    "mem_percent": 6.1,
                    "temp_c": 40,
                    "power_w": 20,
                }
            ],
            "offline": True,
        },
    ]

    for h in hosts:
        for i in range(args.points):
            ts = now - (args.points - i) * 60
            wave = (i % 7) * 4
            cpu = min(99, h["base_cpu"] + wave)
            if h["host_id"] == "app-cpu-01" and i == args.points - 1:
                cpu = 96  # 触发 CPU critical
            npus = [dict(x) for x in h["npus"]]
            if h["host_id"] == "npu-lab-01" and i == args.points - 1:
                npus[1]["temp_c"] = 96
                npus[1]["util_percent"] = 97
            payload = {
                "host_id": h["host_id"],
                "hostname": h["hostname"],
                "host_type": h["host_type"],
                "timestamp": ts - (3600 if h.get("offline") else 0),
                "system": {
                    "cpu_percent": cpu,
                    "cpu_count": h["cpu_count"],
                    "mem_total_mb": 256000,
                    "mem_used_mb": 120000,
                    "mem_percent": 46.8,
                    "disk_total_gb": 3600,
                    "disk_used_gb": (1920 if h["host_id"] == "app-cpu-01" and i == args.points - 1 else 500) + 700,
                    "disk_percent": (
                        96.0
                        if h["host_id"] == "app-cpu-01" and i == args.points - 1
                        else 43.8
                    ),
                    "disk_count": 2,
                    "disks": [
                        {
                            "mount": "/data",
                            "device": "/dev/sdb1",
                            "fstype": "xfs",
                            "total_gb": 2000,
                            "used_gb": 1920 if h["host_id"] == "app-cpu-01" and i == args.points - 1 else 500,
                            "percent": 96.0 if h["host_id"] == "app-cpu-01" and i == args.points - 1 else 25.0,
                        },
                        {
                            "mount": "/",
                            "device": "/dev/sda1",
                            "fstype": "ext4",
                            "total_gb": 1600,
                            "used_gb": 700,
                            "percent": 43.8,
                        },
                    ],
                    "load1": round(h["cpu_count"] * 0.4 + i * 0.05, 2),
                    "uptime_sec": 200000 + i,
                    "net_rx_mbps": round(8 + (i % 5) * 12.5, 2),
                    "net_tx_mbps": round(2 + (i % 3) * 4.2, 2),
                    "net_rated_mbps": 1000,
                    "net_link_mbps": 1000,
                    "net_rx_percent": round((8 + (i % 5) * 12.5) / 10.0, 2),
                    "net_tx_percent": round((2 + (i % 3) * 4.2) / 10.0, 2),
                },
                "npus": npus,
                "gpus": [],
                "gpu_processes": [],
                "agent_version": "1.2.0-demo",
            }
            post(args.url, payload)

    print("演示数据已写入：%s 台 × %s 点（含 CPU/NPU 异常场景）" % (len(hosts), args.points))
    print("打开 http://127.0.0.1:8080/ 查看")


if __name__ == "__main__":
    main()
