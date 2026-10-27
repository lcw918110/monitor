"""中心端 HTTP 服务（标准库 http.server）。"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from urllib.parse import unquote, urlparse

from center import __version__
from center import api as api_mod
from center import deploy_api
from center import reports as reports_mod
from center.admin_settings import (
    AdminSettingsStore,
    _UPDATE_FIELDS,
    bootstrap_management_token,
    handle_get_admin_settings,
    write_allowed,
)
from center.center_params import (
    CenterRuntime,
    _cfg_int,
    merge_center_config,
    period_util_thresholds,
    read_user_config,
)
from center.deploy_runner import DeployRunner
from center.report_push import set_push_store
from center.report_schedule import ReportScheduler, deliver_report
from center.deploy_store import DeployStore
from center.storage import Storage
from common.validate import validate_center_config


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.isfile(path):
        return merge_center_config(None)
    with open(path, "r", encoding="utf-8") as handle:
        user = json.load(handle)
    if not isinstance(user, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return merge_center_config(user)


def handle_settings_write(
    store: Optional[AdminSettingsStore],
    body: bytes,
    runtime: Optional[CenterRuntime] = None,
) -> Tuple[int, Dict[str, Any]]:
    """管理 Token / 推送与中心参数可以分次保存，也可以放在同一次请求里。"""
    if store is None:
        return 503, {"ok": False, "error": "管理设置未初始化"}
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return 400, {"ok": False, "error": "请求体必须是合法 JSON"}
    if not isinstance(data, dict):
        return 400, {"ok": False, "error": "JSON 根节点必须是对象"}
    has_center = "center" in data
    center = data.get("center")
    admin_body = {key: data[key] for key in _UPDATE_FIELDS if key in data}
    if not admin_body and not has_center:
        return 400, {"ok": False, "error": "没有要保存的设置"}
    if has_center:
        if runtime is None:
            return 503, {"ok": False, "error": "中心配置未初始化"}
        if not isinstance(center, dict):
            return 400, {"ok": False, "error": "center 必须是对象"}
        error = runtime.validate(center)
        if error:
            return 400, {"ok": False, "error": error}
    admin_payload: Optional[Dict[str, Any]] = None
    if admin_body:
        code, admin_payload = store.update(admin_body)
        if code != 200:
            return code, admin_payload
    center_payload: Optional[Dict[str, Any]] = None
    if has_center and runtime is not None:
        code, center_payload = runtime.apply(center)
        if code != 200:
            return code, center_payload
    if admin_payload is None:
        code, admin_payload = handle_get_admin_settings(store)
        if code != 200:
            return code, admin_payload
    result = dict(admin_payload or {})
    if center_payload is not None:
        result["center"] = center_payload.get("center")
        result["restart_required"] = list(center_payload.get("restart_required") or [])
    return 200, result


class CleanupControl:
    """清理线程每轮读这个间隔，设置页改完不用重启。"""

    def __init__(self, interval: int) -> None:
        self.interval = interval


def make_handler(
    storage: Storage,
    token: str,
    anomaly_thresholds: Optional[Dict[str, Any]] = None,
    deploy_store: Optional[DeployStore] = None,
    deploy_runner: Optional[DeployRunner] = None,
    period_util: Optional[Dict[str, Any]] = None,
    admin_store: Optional[AdminSettingsStore] = None,
    runtime: Optional[CenterRuntime] = None,
):
    thresholds = anomaly_thresholds or {}
    busy_defaults = period_util_thresholds(period_util)
    dstore = deploy_store
    drunner = deploy_runner

    def current_anomaly() -> Dict[str, Any]:
        if runtime is not None:
            return runtime.anomaly()
        return thresholds

    def current_busy() -> Dict[str, float]:
        if runtime is not None:
            return runtime.busy_thresholds()
        return busy_defaults

    def current_token() -> str:
        if runtime is not None:
            return runtime.agent_token()
        return token or ""

    def current_report_rules() -> Optional[Dict[str, Any]]:
        if runtime is not None:
            return runtime.report_section()
        return None

    class Handler(BaseHTTPRequestHandler):
        server_version = "MonitorCenter/" + __version__

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

        def _allow_management_write(self) -> bool:
            presented = (self.headers.get("X-Management-Token") or "").strip()
            if write_allowed(admin_store, presented):
                return True
            self._send_json(
                401,
                {
                    "ok": False,
                    "error": "管理 Token 无效或未提供",
                    "code": "management_token_required",
                },
            )
            return False

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)

            if path == "/api/v1/hosts":
                code, payload = api_mod.handle_hosts_list(
                    storage, thresholds=current_anomaly(), query=parsed.query
                )
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
                        storage, host_id, thresholds=current_anomaly()
                    )
                    self._send_json(code, payload)
                    return
                if len(parts) == 2 and parts[1] == "history":
                    code, payload = api_mod.handle_host_history(
                        storage, host_id, query=parsed.query, busy_defaults=current_busy()
                    )
                    self._send_json(code, payload)
                    return
                if len(parts) == 2 and parts[1] == "period-stats":
                    code, payload = api_mod.handle_host_period_stats(
                        storage,
                        host_id,
                        query=parsed.query,
                        busy_defaults=current_busy(),
                    )
                    self._send_json(code, payload)
                    return
                self._send_json(404, {"ok": False, "error": "未找到接口"})
                return

            if path == "/api/v1/period-stats":
                code, payload = api_mod.handle_cluster_period_stats(
                    storage, query=parsed.query, busy_defaults=current_busy()
                )
                self._send_json(code, payload)
                return

            if path == "/api/v1/stats":
                code, payload = api_mod.handle_stats(storage)
                self._send_json(code, payload)
                return

            if path == "/api/v1/anomaly":
                code, payload = api_mod.handle_anomaly(storage, thresholds=current_anomaly())
                self._send_json(code, payload)
                return

            if path == "/api/v1/export/period-stats.csv":
                code, text = api_mod.handle_export_period_csv(
                    storage, query=parsed.query, busy_defaults=current_busy()
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
                    dstore, current_token(), parsed.query
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

            if path == "/api/v1/reports/preview":
                code, payload = reports_mod.handle_report_preview(
                    storage, query=parsed.query
                )
                self._send_json(code, payload)
                return

            if path == "/api/v1/reports":
                code, payload = reports_mod.handle_list_reports(
                    storage, query=parsed.query
                )
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/reports/"):
                rest = path[len("/api/v1/reports/") :].strip("/")
                parts = [p for p in rest.split("/") if p]
                if not parts:
                    self._send_json(404, {"ok": False, "error": "未找到接口"})
                    return
                try:
                    report_id = int(parts[0])
                except ValueError:
                    self._send_json(400, {"ok": False, "error": "非法 report id"})
                    return
                if len(parts) == 1:
                    code, payload = reports_mod.handle_get_report(storage, report_id)
                    self._send_json(code, payload)
                    return
                if len(parts) == 2 and parts[1] == "markdown":
                    code, payload = reports_mod.handle_report_markdown(storage, report_id)
                    if code != 200:
                        self._send_json(code, payload)
                        return
                    data = payload["markdown"].encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/markdown; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="%s"' % payload["filename"],
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                if len(parts) == 2 and parts[1] == "rankings.csv":
                    code, payload = reports_mod.handle_report_csv(storage, report_id)
                    if code != 200:
                        self._send_json(code, payload)
                        return
                    data = payload["csv"].encode("utf-8-sig")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/csv; charset=utf-8")
                    self.send_header(
                        "Content-Disposition",
                        'attachment; filename="%s"' % payload["filename"],
                    )
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self._send_json(404, {"ok": False, "error": "未找到接口"})
                return

            if path == "/api/v1/settings":
                code, payload = handle_get_admin_settings(admin_store)
                if code == 200 and runtime is not None:
                    payload = dict(payload)
                    payload["center"] = runtime.public_view()
                self._send_json(code, payload)
                return

            if path == "/api/v1/health":
                self._send_json(
                    200,
                    {"ok": True, "version": __version__, "name": "简易多机监控"},
                )
                return

            self._serve_static(path)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            body = self._read_body()
            # Agent 上报仍只校验 center.json 的 token，不要求管理 Token。
            if path.startswith("/api/") and path != "/api/v1/metrics":
                if not self._allow_management_write():
                    return

            if path == "/api/v1/settings":
                code, payload = handle_settings_write(admin_store, body, runtime)
                self._send_json(code, payload)
                return

            if path == "/api/v1/reports/generate":
                code, payload = reports_mod.handle_generate_report(
                    storage,
                    body,
                    query=parsed.query,
                    busy_thresholds=current_busy(),
                    anomaly_thresholds=current_anomaly(),
                    report_rules=current_report_rules(),
                )
                report = (payload or {}).get("report") or {}
                if (
                    code == 200
                    and not payload.get("skipped")
                    and report.get("status", "ok") == "ok"
                ):
                    payload["push"] = deliver_report(report)
                self._send_json(code, payload)
                return

            if path == "/api/v1/metrics":
                remote_ip = ""
                try:
                    remote_ip = (self.client_address or ("", 0))[0]
                except (TypeError, IndexError):
                    remote_ip = ""
                code, payload = api_mod.handle_metrics_post(
                    storage, body, expected_token=current_token(), remote_ip=remote_ip
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

            if path == "/api/v1/hosts/meta/import":
                code, payload = api_mod.handle_host_meta_import(
                    storage, body, query=parsed.query
                )
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/hosts/") and path.endswith("/readdress/undo"):
                host_id = path[len("/api/v1/hosts/") : -len("/readdress/undo")].strip("/")
                if not host_id or "/" in host_id or ".." in host_id:
                    self._send_json(400, {"ok": False, "error": "非法 host_id"})
                    return
                code, payload = api_mod.handle_host_readdress_undo(
                    storage, host_id, body, deploy_store=dstore
                )
                self._send_json(code, payload)
                return

            if path.startswith("/api/v1/hosts/") and (
                path.endswith("/group")
                or path.endswith("/meta")
                or path.endswith("/hide")
                or path.endswith("/restore")
                or path.endswith("/readdress")
            ):
                rest = path[len("/api/v1/hosts/") :].strip("/")
                parts = rest.split("/") if rest else []
                if len(parts) != 2 or not parts[0] or ".." in parts[0]:
                    self._send_json(400, {"ok": False, "error": "非法 host_id"})
                    return
                host_id, action = parts
                if action == "group":
                    code, payload = api_mod.handle_host_assign_group(
                        storage, host_id, body
                    )
                elif action == "meta":
                    code, payload = api_mod.handle_host_meta_update(
                        storage, host_id, body
                    )
                elif action == "hide":
                    code, payload = api_mod.handle_host_hide(
                        storage, host_id, body, deploy_store=dstore
                    )
                elif action == "readdress":
                    code, payload = api_mod.handle_host_readdress(
                        storage, host_id, body, deploy_store=dstore
                    )
                else:
                    code, payload = api_mod.handle_host_restore(storage, host_id)
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
            if path.startswith("/api/"):
                if not self._allow_management_write():
                    return
            if path.startswith("/api/v1/hosts/"):
                rest = path[len("/api/v1/hosts/") :].strip("/")
                parts = rest.split("/") if rest else []
                if len(parts) != 1 or not parts[0] or ".." in parts[0]:
                    self._send_json(400, {"ok": False, "error": "非法 host_id"})
                    return
                code, payload = api_mod.handle_host_delete(
                    storage,
                    parts[0],
                    query=parsed.query,
                    deploy_store=dstore,
                )
                self._send_json(code, payload)
                return
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


def _cleanup_loop(storage: Storage, control: CleanupControl) -> None:
    while True:
        try:
            deleted = storage.cleanup_old_metrics()
            if deleted:
                print("[cleanup] 删除过期历史上报 %s 条" % deleted, flush=True)
        except Exception as exc:  # noqa: BLE001
            print("[cleanup] 失败: %s" % exc, flush=True)
        try:
            interval = int(control.interval)
        except (TypeError, ValueError):
            interval = 3600
        time.sleep(max(60, interval))


def run_server(
    config: Dict[str, Any],
    config_path: Optional[str] = None,
    user_doc: Optional[Dict[str, Any]] = None,
) -> None:
    data_dir = config.get("data_dir") or "data"
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "monitor.db")
    storage = Storage(
        db_path=db_path,
        offline_seconds=_cfg_int(config.get("offline_seconds"), 90),
        retention_days=_cfg_int(config.get("retention_days"), 7),
    )

    cleanup_interval = _cfg_int(config.get("cleanup_interval_seconds"), 3600)
    cleanup = CleanupControl(cleanup_interval)
    t = threading.Thread(
        target=_cleanup_loop,
        args=(storage, cleanup),
        daemon=True,
        name="cleanup",
    )
    t.start()

    host = config.get("host") or "0.0.0.0"
    port = int(config.get("port") or 8080)
    token = config.get("token") or ""
    admin_store = AdminSettingsStore(db_path)
    boot = bootstrap_management_token(
        admin_store, os.environ.get("MONITOR_MANAGEMENT_TOKEN") or ""
    )
    if boot.startswith("invalid:"):
        print("[配置错误] MONITOR_MANAGEMENT_TOKEN %s" % boot.split(":", 1)[1], flush=True)
        raise SystemExit(2)
    if boot == "applied":
        print("[设置] 已从环境变量写入管理 Token（只存哈希，不明文落盘）", flush=True)
    elif boot == "kept":
        print("[设置] 管理 Token 已存在，忽略环境变量", flush=True)
    set_push_store(admin_store)
    if admin_store.token_configured():
        print("[设置] 管理 Token 已启用。写操作需要请求头 X-Management-Token。", flush=True)
    else:
        print(
            "[设置] 尚未设置管理 Token，写操作暂时开放。请打开 /settings.html 立刻设置。",
            flush=True,
        )
    push_view = admin_store.public_view()
    if push_view.get("push_enabled") and push_view.get("push_webhook_set"):
        print(
            "[设置] 诊断报告将推送到%s" % push_view.get("push_channel_label"),
            flush=True,
        )
    else:
        print("[设置] 诊断报告推送未配置，生成后不外发。", flush=True)

    scheduler = ReportScheduler(
        storage,
        schedule=config.get("report_schedule"),
        busy_thresholds=period_util_thresholds(config.get("period_util") or {}),
        anomaly_thresholds=config.get("anomaly") or {},
        report_rules=config.get("report") or {},
    )
    scheduler.start()

    deploy_store = DeployStore(db_path)
    from center.python_bundle import bundles_dir_from_config

    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    deploy_runner = DeployRunner(
        deploy_store,
        token=token,
        bundles_dir=bundles_dir_from_config(config, project_root),
    )
    from center.netutil import ensure_public_center_url

    public_url = ensure_public_center_url(deploy_store, port)
    if public_url:
        print("[部署] 中心对外访问地址: %s（供客户端 Agent 上报，一般不用 127.0.0.1）" % public_url, flush=True)
    else:
        print(
            "[提示] 未能自动推断局域网 IP，请在部署页填写中心对外地址，例如 http://<本机IP>:%s" % port,
            flush=True,
        )
    runtime = None
    if config_path:
        runtime = CenterRuntime(
            user_doc if isinstance(user_doc, dict) else {},
            config_path,
            storage=storage,
            scheduler=scheduler,
            deploy_runner=deploy_runner,
            cleanup=cleanup,
            project_root=project_root,
        )
    handler = make_handler(
        storage,
        token,
        anomaly_thresholds=config.get("anomaly") or {},
        deploy_store=deploy_store,
        deploy_runner=deploy_runner,
        period_util=config.get("period_util") or {},
        admin_store=admin_store,
        runtime=runtime,
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    print("中心端已启动: http://%s:%s/" % (host, port), flush=True)
    print("客户端部署页: http://%s:%s/deploy.html" % (host, port), flush=True)
    print("管理设置页: http://%s:%s/settings.html" % (host, port), flush=True)
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
    read_path = args.config if args.config and os.path.isfile(args.config) else None
    if args.config and not read_path:
        example = "config/center.example.json"
        if os.path.isfile(example):
            print("未找到 %s，使用 %s" % (args.config, example))
            read_path = example
        else:
            print("未找到配置，使用内置默认值")
    user_doc = read_user_config(read_path) if read_path else {}
    cfg = load_config(read_path)
    ok, errors = validate_center_config(cfg)
    if not ok:
        for err in errors:
            print("[配置错误] %s" % err)
        raise SystemExit(2)
    # 保存写到启动参数指定的 center.json，不改 example。
    run_server(cfg, config_path=args.config, user_doc=user_doc)


if __name__ == "__main__":
    main()
