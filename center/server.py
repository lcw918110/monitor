"""中心端 HTTP 服务（标准库 http.server）。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import unquote, urlparse

from center import api as api_mod
from center import deploy_api
from center.deploy_runner import DeployRunner
from center.deploy_store import DeployStore
from center.storage import Storage
from common.validate import validate_center_config


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def load_config(path: Optional[str]) -> Dict[str, Any]:
    cfg = {
        "host": "0.0.0.0",
        "port": 8080,
        "data_dir": "data",
        "token": "",
        "offline_seconds": 90,
        "retention_days": 7,
        "cleanup_interval_seconds": 3600,
        "anomaly": {
            "cpu_warn_percent": 80,
            "cpu_critical_percent": 95,
            "cpu_load_per_core_warn": 2.0,
            "mem_warn_percent": 85,
            "mem_critical_percent": 95,
            "disk_warn_percent": 85,
            "disk_critical_percent": 95,
            "accel_util_warn_percent": 95,
            "accel_temp_warn_c": 85,
            "accel_temp_critical_c": 95,
            "accel_mem_warn_percent": 90,
            "accel_mem_critical_percent": 98,
        },
        "period_util": {
            "busy_cpu_percent": 80,
            "busy_accel_percent": 80,
        },
    }
    if path and os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
        if not isinstance(user, dict):
            raise ValueError("配置文件根节点必须是 JSON 对象")
        anomaly = cfg.get("anomaly") or {}
        period_util = cfg.get("period_util") or {}
        user_anomaly = user.pop("anomaly", None)
        user_period_util = user.pop("period_util", None)
        # 兼容旧 alerts 字段名，忽略不用
        user.pop("alerts", None)
        cfg.update(user)
        if isinstance(user_anomaly, dict):
            anomaly.update(user_anomaly)
        cfg["anomaly"] = anomaly
        if isinstance(user_period_util, dict):
            period_util.update(user_period_util)
        cfg["period_util"] = period_util
    return cfg


def period_util_thresholds(period_util: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    from center.storage import DEFAULT_BUSY_THRESHOLDS

    thresholds = dict(DEFAULT_BUSY_THRESHOLDS)
    cfg = period_util or {}
    mapping = {
        "busy_cpu_percent": "cpu_percent",
        "busy_accel_percent": "accel_util_avg",
        "busy_mem_percent": "mem_percent",
        "busy_disk_percent": "disk_percent",
    }
    for cfg_key, metric in mapping.items():
        if cfg.get(cfg_key) is None or cfg.get(cfg_key) == "":
            continue
        try:
            value = float(cfg[cfg_key])
        except (TypeError, ValueError):
            continue
        if value < 0:
            thresholds.pop(metric, None)
        else:
            thresholds[metric] = value
    return thresholds


def make_handler(
    storage: Storage,
    token: str,
    anomaly_thresholds: Optional[Dict[str, Any]] = None,
    deploy_store: Optional[DeployStore] = None,
    deploy_runner: Optional[DeployRunner] = None,
    period_util: Optional[Dict[str, Any]] = None,
):
    thresholds = anomaly_thresholds or {}
    busy_defaults = period_util_thresholds(period_util)
    dstore = deploy_store
    drunner = deploy_runner

    class Handler(BaseHTTPRequestHandler):
        server_version = "MonitorCenter/1.3"

        def log_message(self, fmt: str, *args: Any) -> None:
            print("[%s] %s" % (self.log_date_time_string(), fmt % args), flush=True)

        def _send_json(self, code: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _send_bytes(self, code: int, data: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self) -> bytes:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return b""
            return self.rfile.read(length)

        def _require_deploy(self) -> bool:
            if dstore is None or drunner is None:
                self._send_json(500, {"ok": False, "error": "部署模块未初始化"})
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path == "/api/v1/hosts":
                code, payload = api_mod.handle_hosts_list(storage, thresholds=thresholds)
                self._send_json(code, payload)
                return

            if path == "/api/v1/groups":
                code, payload = api_mod.handle_groups_list(storage)
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/hosts/"):
                rest = path[len("/api/v1/hosts/") :].strip("/")
                parts = rest.split("/") if rest else []
                if not parts or not parts[0] or ".." in parts[0]:
                    self._send_json(400, {"ok": False, "error": "非法 host_id"})
                    return
                host_id = parts[0]
                if len(parts) == 1:
                    code, payload = api_mod.handle_host_detail(
                        storage, host_id, thresholds=thresholds
                    )
                    self._send_json(code, payload)
                    return
                if len(parts) == 2 and parts[1] == "history":
                    code, payload = api_mod.handle_host_history(
                        storage, host_id, query=parsed.query, busy_defaults=busy_defaults
                    )
                    self._send_json(code, payload)
                    return
                if len(parts) == 2 and parts[1] == "period-stats":
                    code, payload = api_mod.handle_host_period_stats(
                        storage,
                        host_id,
                        query=parsed.query,
                        busy_defaults=busy_defaults,
                    )
                    self._send_json(code, payload)
                    return
                self._send_json(404, {"ok": False, "error": "未找到接口"})
                return

            if path == "/api/v1/period-stats":
                code, payload = api_mod.handle_cluster_period_stats(
                    storage, query=parsed.query, busy_defaults=busy_defaults
                )
                self._send_json(code, payload)
                return

            if path == "/api/v1/stats":
                code, payload = api_mod.handle_stats(storage)
                self._send_json(code, payload)
                return

            if path == "/api/v1/anomaly":
                code, payload = api_mod.handle_anomaly(storage, thresholds=thresholds)
                self._send_json(code, payload)
                return

            if path == "/api/v1/export/period-stats.csv":
                code, text = api_mod.handle_export_period_csv(
                    storage, query=parsed.query, busy_defaults=busy_defaults
                )
                if code != 200:
                    self._send_json(code, text)
                    return
                data = text.encode("utf-8-sig")
                self.send_response(code)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    'attachment; filename="period-stats.csv"',
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/v1/export/hosts.json":
                code, payload = api_mod.handle_export_json(storage)
                self._send_json(code, payload)
                return

            if path == "/api/v1/export/hosts.csv":
                code, text = api_mod.handle_export_csv(storage)
                data = text.encode("utf-8-sig")
                self.send_response(code)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition", 'attachment; filename="hosts.csv"'
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            # ---- deploy APIs ----
            if path == "/api/v1/deploy/settings":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_get_settings(dstore)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/targets":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_list_targets(dstore)
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/deploy/targets/"):
                if not self._require_deploy():
                    return
                raw_id = path[len("/api/v1/deploy/targets/") :].strip("/")
                try:
                    tid = int(raw_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 id"})
                    return
                code, payload = deploy_api.handle_get_target(dstore, tid)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/jobs":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_list_jobs(dstore)
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/deploy/jobs/"):
                if not self._require_deploy():
                    return
                raw_id = path[len("/api/v1/deploy/jobs/") :].strip("/")
                try:
                    job_id = int(raw_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 job id"})
                    return
                code, payload = deploy_api.handle_get_job(dstore, job_id)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/install-script":
                if not self._require_deploy():
                    return
                code, text, ctype = deploy_api.handle_install_script(
                    dstore, token, parsed.query
                )
                data = text.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                if code == 200:
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="install-agent.sh"',
                    )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/v1/deploy/template.xlsx":
                if not self._require_deploy():
                    return
                code, data, ctype, filename = deploy_api.handle_excel_template()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header(
                    "Content-Disposition", 'attachment; filename="%s"' % filename
                )
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return

            if path == "/api/v1/network/results":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_list_probe_results(dstore)
                self._send_json(code, payload)
                return

            if path == "/api/v1/network/hosts":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_list_net_hosts(dstore)
                self._send_json(code, payload)
                return

            if path == "/api/v1/health":
                self._send_json(
                    200,
                    {"ok": True, "version": "1.3.1", "name": "简易多机监控"},
                )
                return

            self._serve_static(path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            body = self._read_body()

            if path == "/api/v1/metrics":
                remote_ip = ""
                try:
                    remote_ip = (self.client_address or ("", 0))[0]
                except (TypeError, IndexError):
                    remote_ip = ""
                code, payload = api_mod.handle_metrics_post(
                    storage, body, expected_token=token, remote_ip=remote_ip
                )
                self._send_json(code, payload)
                return

            if path == "/api/v1/groups":
                code, payload = api_mod.handle_group_create(storage, body)
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/groups/"):
                raw_id = path[len("/api/v1/groups/") :].strip("/")
                try:
                    gid = int(raw_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 group id"})
                    return
                code, payload = api_mod.handle_group_rename(storage, gid, body)
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/hosts/") and path.endswith("/group"):
                rest = path[len("/api/v1/hosts/") :].strip("/")
                parts = rest.split("/") if rest else []
                if len(parts) != 2 or parts[1] != "group" or not parts[0]:
                    self._send_json(400, {"ok": False, "error": "非法 host_id"})
                    return
                code, payload = api_mod.handle_host_assign_group(
                    storage, parts[0], body
                )
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/settings":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_save_settings(dstore, body)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/targets":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_add_target(dstore, body)
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/deploy/targets/") and path.count("/") >= 5:
                # POST /api/v1/deploy/targets/{id} 更新（含指定机器）
                if not self._require_deploy():
                    return
                raw_id = path[len("/api/v1/deploy/targets/") :].strip("/")
                if raw_id in ("import", "import-excel"):
                    pass
                else:
                    try:
                        tid = int(raw_id)
                    except ValueError:
                        self._send_json(400, {"ok": False, "error": "非法 id"})
                        return
                    code, payload = deploy_api.handle_update_target(dstore, tid, body)
                    self._send_json(code, payload)
                    return

            if path == "/api/v1/deploy/targets/import":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_import_targets(dstore, body)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/targets/import-excel":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_import_excel(dstore, body)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/run":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_run_deploy(dstore, drunner, body)
                self._send_json(code, payload)
                return

            if path == "/api/v1/deploy/test-ssh":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_test_ssh(dstore, body)
                self._send_json(code, payload)
                return

            if path == "/api/v1/network/probe":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_probe_network(dstore, body)
                self._send_json(code, payload)
                return

            if path == "/api/v1/network/hosts":
                if not self._require_deploy():
                    return
                code, payload = deploy_api.handle_add_net_host(dstore, body)
                self._send_json(code, payload)
                return

            self._send_json(404, {"ok": False, "error": "未找到接口"})

        def do_DELETE(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path.startswith("/api/v1/groups/"):
                raw_id = path[len("/api/v1/groups/") :].strip("/")
                try:
                    gid = int(raw_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 group id"})
                    return
                code, payload = api_mod.handle_group_delete(storage, gid)
                self._send_json(code, payload)
                return
            if path.startswith("/api/v1/deploy/targets/"):
                if not self._require_deploy():
                    return
                raw_id = path[len("/api/v1/deploy/targets/") :].strip("/")
                try:
                    tid = int(raw_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 id"})
                    return
                code, payload = deploy_api.handle_delete_target(dstore, tid)
                self._send_json(code, payload)
                return
            if path.startswith("/api/v1/network/hosts/"):
                if not self._require_deploy():
                    return
                raw_id = path[len("/api/v1/network/hosts/") :].strip("/")
                try:
                    hid = int(raw_id)
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 id"})
                    return
                code, payload = deploy_api.handle_delete_net_host(dstore, hid)
                self._send_json(code, payload)
                return
            self._send_json(404, {"ok": False, "error": "未找到接口"})

        def _serve_static(self, path: str) -> None:
            if path in ("", "/"):
                path = "/index.html"
            rel = path.lstrip("/").replace("\\", "/")
            if ".." in rel.split("/"):
                self._send_json(400, {"ok": False, "error": "非法路径"})
                return
            file_path = os.path.normpath(os.path.join(STATIC_DIR, rel))
            if not file_path.startswith(os.path.normpath(STATIC_DIR)):
                self._send_json(400, {"ok": False, "error": "非法路径"})
                return
            if not os.path.isfile(file_path):
                self._send_json(404, {"ok": False, "error": "页面不存在"})
                return
            ctype, _ = mimetypes.guess_type(file_path)
            if not ctype:
                ctype = "application/octet-stream"
            if ctype.startswith("text/") or ctype in (
                "application/javascript",
                "application/json",
            ):
                ctype = ctype + "; charset=utf-8"
            with open(file_path, "rb") as f:
                data = f.read()
            self._send_bytes(200, data, ctype)

    return Handler


def _cleanup_loop(storage: Storage, interval: int) -> None:
    while True:
        try:
            deleted = storage.cleanup_old_metrics()
            if deleted:
                print("[cleanup] 删除过期历史上报 %s 条" % deleted, flush=True)
        except Exception as exc:  # noqa: BLE001
            print("[cleanup] 失败: %s" % exc, flush=True)
        time.sleep(max(60, interval))


def run_server(config: Dict[str, Any]) -> None:
    data_dir = config.get("data_dir") or "data"
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "monitor.db")
    storage = Storage(
        db_path=db_path,
        offline_seconds=int(config.get("offline_seconds") or 90),
        retention_days=int(config.get("retention_days") or 7),
    )

    cleanup_interval = int(config.get("cleanup_interval_seconds") or 3600)
    t = threading.Thread(
        target=_cleanup_loop,
        args=(storage, cleanup_interval),
        daemon=True,
        name="cleanup",
    )
    t.start()

    host = config.get("host") or "0.0.0.0"
    port = int(config.get("port") or 8080)
    token = config.get("token") or ""
    deploy_store = DeployStore(db_path)
    deploy_runner = DeployRunner(deploy_store, token=token)
    from center.netutil import ensure_public_center_url

    public_url = ensure_public_center_url(deploy_store, port)
    if public_url:
        print("[部署] 中心对外访问地址: %s（供客户端 Agent 上报，一般不用 127.0.0.1）" % public_url, flush=True)
    else:
        print(
            "[提示] 未能自动推断局域网 IP，请在部署页填写中心对外地址，例如 http://<本机IP>:%s" % port,
            flush=True,
        )
    handler = make_handler(
        storage,
        token,
        anomaly_thresholds=config.get("anomaly") or {},
        deploy_store=deploy_store,
        deploy_runner=deploy_runner,
        period_util=config.get("period_util") or {},
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    print("中心端已启动: http://%s:%s/" % (host, port), flush=True)
    print("客户端部署页: http://%s:%s/deploy.html" % (host, port), flush=True)
    print("数据文件: %s" % db_path, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止中心端...")
    finally:
        httpd.server_close()


def main(argv: Optional[list] = None) -> None:
    parser = argparse.ArgumentParser(description="简易多机监控 — 中心端")
    parser.add_argument(
        "--config",
        default="config/center.json",
        help="配置文件路径（默认 config/center.json）",
    )
    args = parser.parse_args(argv)
    config_path = args.config if os.path.isfile(args.config) else None
    if args.config and not config_path:
        # 尝试 example
        example = "config/center.example.json"
        if os.path.isfile(example):
            print("未找到 %s，使用 %s" % (args.config, example))
            config_path = example
        else:
            print("未找到配置，使用内置默认值")
    cfg = load_config(config_path)
    ok, errors = validate_center_config(cfg)
    if not ok:
        for err in errors:
            print("[配置错误] %s" % err)
        raise SystemExit(2)
    run_server(cfg)


if __name__ == "__main__":
    main()
