"""采集端主程序。"""

import argparse
import json
import os
import socket
import time
from typing import Any, Dict, List, Optional, Tuple

from agent import __version__ as AGENT_VERSION
from agent.metrics.accelerators import (
    collect_accelerators,
    collect_nvidia_processes,
    split_for_payload,
)
from agent.metrics.system import collect_system
from agent.sender import send_metrics
from agent.spool import (
    Spool,
    apply_flush_response,
    batch_url_for,
    build_flush_body,
    clamp_batch_size,
    parse_retention_hours,
    resolve_spool_dir,
    retention_hours_from_body,
)
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
        "spool_retention_hours": 24,
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
        cards: List[Dict[str, Any]] = []
    else:
        cards = collect_accelerators()

    parts = split_for_payload(cards)
    if configured == "auto":
        host_type = resolve_auto_host_type(bool(cards))
    else:
        host_type = configured

    nvidia_cards = [c for c in parts["gpus"] if (c.get("vendor") or "") == "nvidia"]
    gpu_procs = collect_nvidia_processes() if nvidia_cards else []

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
        "agent_version": AGENT_VERSION,
    }
    token = cfg.get("token") or ""
    if token:
        payload["token"] = token
    return payload


def open_spool(cfg: Dict[str, Any], config_path: Optional[str]) -> Spool:
    hours = parse_retention_hours(cfg.get("spool_retention_hours"), 24)
    cap = cfg.get("spool_max_samples")
    return Spool(
        resolve_spool_dir(cfg, config_path),
        retention_hours=hours,
        max_samples=cap if cap not in (None, "") else None,
        batch_size=clamp_batch_size(cfg.get("spool_batch_size") or 80),
    )


def submit_payload(
    cfg: Dict[str, Any],
    payload: Dict[str, Any],
    spool: Spool,
    send_fn: Any = None,
) -> Tuple[int, str, str]:
    """先送当前样本。失败则入队。成功则最多再补一批，避免把积压一次打满中心。"""
    sender = send_fn or send_metrics
    url = cfg.get("center_url") or "http://127.0.0.1:8080/api/v1/metrics"
    code, body = sender(url, payload)
    if 200 <= int(code) < 300:
        hours = retention_hours_from_body(body)
        if hours is not None:
            spool.set_retention_hours(hours)
        _flush_one_batch(cfg, spool, sender, payload)
        return int(code), body, "sent"
    state = spool.enqueue(payload)
    return int(code), body, state


def _flush_one_batch(cfg: Dict[str, Any], spool: Spool, sender: Any, payload: Dict[str, Any]) -> None:
    token = str(cfg.get("token") or payload.get("token") or "")
    built = build_flush_body(
        spool,
        token=token,
        host_id=str(payload.get("host_id") or ""),
        limit=spool.batch_size,
    )
    if built is None:
        return
    body, stamps = built
    code, resp = sender(batch_url_for(cfg.get("center_url") or ""), body)
    apply_flush_response(spool, stamps, code, resp)
    if not (200 <= int(code) < 300):
        print(
            "[%s] 补报未完成 code=%s 队列仍保留"
            % (time.strftime("%H:%M:%S"), code)
        )


def run_loop(cfg: Dict[str, Any], config_path: Optional[str] = None) -> None:
    url = cfg.get("center_url") or "http://127.0.0.1:8080/api/v1/metrics"
    interval = max(5, int(cfg.get("interval_seconds") or 15))
    ident = resolve_identity(cfg)
    spool = open_spool(cfg, config_path)
    print(
        "Agent 启动: host_id=%s → %s (间隔 %ss，断网样本留 %sh)"
        % (ident["host_id"], url, interval, spool.retention_hours)
    )
    while True:
        started = time.time()
        try:
            payload = build_payload(cfg)
            code, body, state = submit_payload(cfg, payload, spool)
            sysinfo = payload.get("system") or {}
            acc = payload.get("accelerators") or []
            if state == "sent":
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
            elif state == "overflow":
                print(
                    "[%s] 上报失败且本地队列已满，最旧样本已丢弃 code=%s"
                    % (time.strftime("%H:%M:%S"), code)
                )
            else:
                print(
                    "[%s] 上报失败，已写入本地队列 code=%s body=%s"
                    % (time.strftime("%H:%M:%S"), code, body[:200])
                )
        except Exception as exc:  # noqa: BLE001
            print("[%s] 采集/上报异常: %s" % (time.strftime("%H:%M:%S"), exc))

        elapsed = time.time() - started
        time.sleep(max(1.0, interval - elapsed))


def main(argv: Optional[List[str]] = None) -> None:
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

    spool = open_spool(cfg, config_path)
    if args.once:
        payload = build_payload(cfg)
        code, body, state = submit_payload(cfg, payload, spool)
        print("once code=%s body=%s spool=%s" % (code, body, state))
        if state != "sent":
            raise SystemExit(1)
        return

    run_loop(cfg, config_path)


if __name__ == "__main__":
    main()
