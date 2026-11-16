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
    apply_flush_result,
    batch_url,
    build_batch,
    parse_response,
    resolve_spool_dir,
    should_spool,
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
        "spool_batch_size": 80,
        "data_dir": "",
        "spool_dir": "",
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


def open_spool(cfg: Dict[str, Any]) -> Spool:
    return Spool(
        resolve_spool_dir(cfg),
        retention_hours=cfg.get("spool_retention_hours") or 24,
        batch_size=cfg.get("spool_batch_size") or 80,
    )


def remember_retention(spool: Spool, body: str) -> None:
    response = parse_response(body)
    if response.get("spool_retention_hours") is not None:
        spool.note_retention_hint(response.get("spool_retention_hours"))


def flush_spool(cfg: Dict[str, Any], spool: Spool) -> None:
    """成功上报之后只补一小批，避免恢复瞬间把中心打满。剩下的等下个周期。"""
    body = build_batch(spool, token=str(cfg.get("token") or ""))
    samples = body.get("samples") or []
    overflows = body.get("overflows") or []
    if not samples and not overflows:
        return
    code, text = send_metrics(
        batch_url(cfg.get("center_url")),
        body,
        retries=1,
        retry_delay=0.5,
    )
    ok = apply_flush_result(spool, len(samples), len(overflows), code, parse_response(text))
    if ok:
        print(
            "[%s] 补报 %s 条，溢出记录 %s 条，队列剩余 %s"
            % (time.strftime("%H:%M:%S"), len(samples), len(overflows), spool.pending_count())
        )
    else:
        print(
            "[%s] 补报未成功 code=%s，本地仍保留 %s 条"
            % (time.strftime("%H:%M:%S"), code, spool.pending_count())
        )


def deliver_payload(cfg: Dict[str, Any], spool: Spool, payload: Dict[str, Any]) -> Tuple[int, str]:
    url = cfg.get("center_url") or "http://127.0.0.1:8080/api/v1/metrics"
    code, body = send_metrics(url, payload)
    if 200 <= code < 300:
        remember_retention(spool, body)
        try:
            flush_spool(cfg, spool)
        except Exception as exc:  # noqa: BLE001
            print("[%s] 补报异常: %s" % (time.strftime("%H:%M:%S"), exc))
        return code, body
    if should_spool(code):
        try:
            spool.enqueue(payload)
            print(
                "[%s] 上报失败 code=%s，已按采集时间 %s 写入本地暂存（待发 %s）"
                % (
                    time.strftime("%H:%M:%S"),
                    code,
                    payload.get("timestamp"),
                    spool.pending_count(),
                )
            )
        except Exception as exc:  # noqa: BLE001
            print("[%s] 上报失败且暂存写入失败: %s" % (time.strftime("%H:%M:%S"), exc))
    else:
        print(
            "[%s] 上报失败 code=%s body=%s"
            % (time.strftime("%H:%M:%S"), code, (body or "")[:200])
        )
    return code, body


def run_loop(cfg: Dict[str, Any]) -> None:
    url = cfg.get("center_url") or "http://127.0.0.1:8080/api/v1/metrics"
    interval = max(5, int(cfg.get("interval_seconds") or 15))
    ident = resolve_identity(cfg)
    spool = open_spool(cfg)
    print(
        "Agent 启动: host_id=%s → %s (间隔 %ss，暂存 %s，保留 %sh)"
        % (
            ident["host_id"],
            url,
            interval,
            spool.directory,
            spool.effective_retention_hours(),
        )
    )
    while True:
        started = time.time()
        try:
            payload = build_payload(cfg)
            code, _body = deliver_payload(cfg, spool, payload)
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

    if args.once:
        payload = build_payload(cfg)
        code, body = deliver_payload(cfg, open_spool(cfg), payload)
        print("once code=%s body=%s" % (code, body))
        if not (200 <= code < 300):
            raise SystemExit(1)
        return

    run_loop(cfg)


if __name__ == "__main__":
    main()
