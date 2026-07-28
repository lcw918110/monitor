"""采集端主程序。"""

from __future__ import annotations

import argparse
import json
import os
import socket
import time
from typing import Any, Dict, Optional

from agent.metrics.accelerators import (
    collect_accelerators,
    collect_nvidia_processes,
    split_for_payload,
)
from agent.metrics.system import collect_system
from agent.sender import send_metrics
from common.host_type import normalize_host_type, resolve_auto_host_type
from common.validate import validate_agent_config


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = {
        "center_url": "http://127.0.0.1:8080/api/v1/metrics",
        "host_id": "",
        "hostname": "",
        "host_type": "auto",
        "token": "",
        "interval_seconds": 15,
        "disk_path": "/",
    }
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        cfg.update(user)
    cfg["host_type"] = normalize_host_type(cfg.get("host_type"))
    return cfg


def resolve_identity(cfg: Dict[str, Any]) -> Dict[str, str]:
    hostname = cfg.get("hostname") or socket.gethostname()
    host_id = cfg.get("host_id") or hostname
    return {"hostname": hostname, "host_id": host_id}


def build_payload(cfg: Dict[str, Any]) -> Dict[str, Any]:
    ident = resolve_identity(cfg)
    system = collect_system(disk_path=cfg.get("disk_path") or "/")
    configured = normalize_host_type(cfg.get("host_type"))

    # cpu：只采系统指标；gpu / auto：采系统 + 各厂商加速卡
    if configured == "cpu":
        cards: list = []
    else:
        cards = collect_accelerators()

    parts = split_for_payload(cards)
    if configured == "auto":
        host_type = resolve_auto_host_type(bool(cards))
    else:
        host_type = configured

    gpu_procs = collect_nvidia_processes() if parts["gpus"] else []

    payload: Dict[str, Any] = {
        "host_id": ident["host_id"],
        "hostname": ident["hostname"],
        "host_type": host_type,
        "timestamp": int(time.time()),
        "system": system,
        "accelerators": parts["accelerators"],
        "npus": parts["npus"],
        "gpus": parts["gpus"],
        "gpu_processes": gpu_procs,
        "agent_version": "1.4.0",
    }
    token = cfg.get("token") or ""
    if token:
        payload["token"] = token
    return payload


def run_loop(cfg: Dict[str, Any]) -> None:
    url = cfg.get("center_url") or "http://127.0.0.1:8080/api/v1/metrics"
    interval = max(5, int(cfg.get("interval_seconds") or 15))
    ident = resolve_identity(cfg)
    print(
        "Agent 启动: host_id=%s → %s (间隔 %ss)"
        % (ident["host_id"], url, interval)
    )
    while True:
        started = time.time()
        try:
            payload = build_payload(cfg)
            code, body = send_metrics(url, payload)
            sysinfo = payload.get("system") or {}
            acc = payload.get("accelerators") or []
            if 200 <= code < 300:
                print(
                    "[%s] 上报成功 type=%s arch=%s cpu=%.1f%% cores=%s cards=%s"
                    % (
                        time.strftime("%H:%M:%S"),
                        payload.get("host_type"),
                        sysinfo.get("cpu_arch") or "-",
                        float(sysinfo.get("cpu_percent") or 0),
                        sysinfo.get("cpu_count"),
                        len(acc),
                    )
                )
            else:
                print(
                    "[%s] 上报失败 code=%s body=%s"
                    % (time.strftime("%H:%M:%S"), code, body[:200])
                )
        except Exception as exc:  # noqa: BLE001
            print("[%s] 采集/上报异常: %s" % (time.strftime("%H:%M:%S"), exc))

        elapsed = time.time() - started
        time.sleep(max(1.0, interval - elapsed))


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="简易多机监控 — 采集端")
    parser.add_argument(
        "--config",
        default="config/agent.json",
        help="配置文件路径（默认 config/agent.json）",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="只采集上报一次后退出",
    )
    args = parser.parse_args(argv)

    config_path = args.config if os.path.isfile(args.config) else None
    if args.config and not config_path:
        example = "config/agent.example.json"
        if os.path.isfile(example):
            print("未找到 %s，使用 %s" % (args.config, example))
            config_path = example
        else:
            print("未找到配置，使用内置默认值")
    cfg = load_config(config_path)
    ok, errors = validate_agent_config(cfg)
    if not ok:
        for err in errors:
            print("[配置错误] %s" % err)
        raise SystemExit(2)

    if args.once:
        payload = build_payload(cfg)
        code, body = send_metrics(cfg["center_url"], payload)
        print("once code=%s body=%s" % (code, body))
        if not (200 <= code < 300):
            raise SystemExit(1)
        return

    run_loop(cfg)


if __name__ == "__main__":
    main()
