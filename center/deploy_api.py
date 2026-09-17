"""部署相关 API。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from center.deploy_runner import DeployRunner, build_install_script
from center.deploy_store import DEFAULT_AGENT_REMOTE_DIR, DeployStore


def _bad(msg: str, code: int = 400) -> Tuple[int, Dict[str, Any]]:
    return code, {"ok": False, "error": msg}


def _host_from_url_or_ip(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" in text:
        try:
            return (urlparse(text).hostname or "").strip()
        except Exception:  # noqa: BLE001
            return ""
    # 可能带端口
    if text.count(":") == 1 and not text.startswith("["):
        return text.split(":", 1)[0].strip()
    return text


def auto_probe_addresses(
    store: DeployStore,
    *,
    target: Optional[Dict[str, Any]] = None,
    extra_hosts: Optional[List[Dict[str, str]]] = None,
    ssh_port: int = 22,
) -> List[Dict[str, Any]]:
    """对配置的地址自动探查（客户端 IP + 关联地址等），写入结果。"""
    from center.netprobe import probe_host

    jobs: List[Dict[str, Any]] = []
    if target:
        tip = (target.get("ip") or "").strip()
        if tip:
            jobs.append(
                {
                    "host": tip,
                    "name": target.get("hostname") or target.get("host_id") or tip,
                    "source": "client:%s" % (target.get("host_id") or tip),
                    "role": "client",
                    "client_host_id": target.get("host_id") or tip,
                    "client_ip": tip,
                    "ref_id": target.get("id"),
                    "ssh_port": int(target.get("ssh_port") or ssh_port or 22),
                }
            )
        for p in target.get("peer_hosts") or []:
            if isinstance(p, dict):
                host = str(p.get("host") or "").strip()
                name = str(p.get("name") or host).strip()
            else:
                host = str(p).strip()
                name = host
            if not host:
                continue
            jobs.append(
                {
                    "host": host,
                    "name": name,
                    "source": "peer:%s" % (target.get("host_id") or tip),
                    "role": "peer",
                    "client_host_id": target.get("host_id") or tip,
                    "client_ip": tip,
                    "ref_id": None,
                    "ssh_port": 22,
                }
            )
    for h in extra_hosts or []:
        host = _host_from_url_or_ip(str(h.get("host") or ""))
        if not host:
            continue
        jobs.append(
            {
                "host": host,
                "name": h.get("name") or host,
                "source": h.get("source") or "config",
                "role": h.get("role") or "config",
                "client_host_id": "",
                "client_ip": "",
                "ref_id": None,
                "ssh_port": int(h.get("ssh_port") or 22),
            }
        )

    results: List[Dict[str, Any]] = []
    seen = set()
    for j in jobs:
        key = (j["host"], j["source"])
        if key in seen:
            continue
        seen.add(key)
        probe = probe_host(
            j["host"],
            ssh_port=int(j.get("ssh_port") or 22),
            extra_ports=[],
            timeout=2.0,
        )
        probe["client_host_id"] = j.get("client_host_id")
        probe["client_ip"] = j.get("client_ip")
        probe["role"] = j.get("role")
        store.save_probe_result(
            host=j["host"],
            name=j.get("name") or j["host"],
            source=j["source"],
            probe=probe,
        )
        if j.get("ref_id") is not None and j.get("role") == "client":
            store.update_target_network(int(j["ref_id"]), probe)
        results.append({"host": j["host"], "role": j.get("role"), "probe": probe})
    return results


def handle_get_settings(store: DeployStore) -> Tuple[int, Dict[str, Any]]:
    from center.netutil import build_public_center_url, is_loopback_url

    settings = store.get_settings()
    # 端口从已保存 URL 或默认 8080 推断建议值
    port = 8080
    cur = (settings.get("public_center_url") or "").strip()
    if cur:
        try:
            from urllib.parse import urlparse

            p = urlparse(cur)
            if p.port:
                port = int(p.port)
        except Exception:  # noqa: BLE001
            pass
    suggested = build_public_center_url(port) or ""
    # 页面展示：若仍空或 loopback，前端可用 suggested 预填
    return 200, {
        "settings": settings,
        "suggested_public_center_url": suggested,
        "public_center_url_is_loopback": is_loopback_url(cur),
    }


def handle_save_settings(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    if not isinstance(data, dict):
        return _bad("根节点必须是对象")
    settings = store.update_settings(data)
    # 配置了中心对外地址则自动探查该地址
    probes = []
    url = (settings.get("public_center_url") or "").strip()
    if url:
        probes = auto_probe_addresses(
            store,
            extra_hosts=[
                {
                    "host": url,
                    "name": "中心对外地址",
                    "source": "public_center_url",
                    "role": "center",
                    "ssh_port": settings.get("default_ssh_port") or 22,
                }
            ],
            ssh_port=int(settings.get("default_ssh_port") or 22),
        )
    return 200, {"ok": True, "settings": settings, "auto_probe": probes}


def handle_list_targets(store: DeployStore) -> Tuple[int, Dict[str, Any]]:
    return 200, {"targets": store.list_targets()}


def handle_add_target(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    try:
        target = store.add_target(data)
    except ValueError as exc:
        return _bad(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _bad("添加失败: %s" % exc)
    probes = auto_probe_addresses(store, target=target)
    target = store.get_target(int(target["id"])) or target
    return 200, {"ok": True, "target": target, "auto_probe": probes}


def handle_get_target(store: DeployStore, target_id: int) -> Tuple[int, Dict[str, Any]]:
    """编辑用：返回含 SSH 密码的完整目标（仅本机部署控制台使用）。"""
    target = store.get_target(target_id, include_secrets=True)
    if not target:
        return _bad("目标不存在", 404)
    return 200, {"ok": True, "target": target}


def handle_delete_target(store: DeployStore, target_id: int) -> Tuple[int, Dict[str, Any]]:
    if not store.delete_target(target_id):
        return _bad("目标不存在", 404)
    return 200, {"ok": True}


def handle_import_targets(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    text = data.get("text") or ""
    if not text.strip():
        return _bad("text 不能为空")
    settings = store.get_settings()
    n = store.import_targets(
        text,
        defaults={
            "ssh_user": settings.get("default_ssh_user"),
            "ssh_port": settings.get("default_ssh_port"),
            "remote_dir": settings.get("default_remote_dir"),
            "host_type": data.get("host_type") or "auto",
        },
    )
    return 200, {"ok": True, "imported": n, "targets": store.list_targets()}


def handle_run_deploy(
    store: DeployStore,
    runner: DeployRunner,
    body: bytes,
) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    target_ids = data.get("target_ids")
    if target_ids == "all" or target_ids is None:
        ids: Optional[List[int]] = None
    else:
        if not isinstance(target_ids, list):
            return _bad("target_ids 必须是数组或 all")
        ids = [int(x) for x in target_ids]
    try:
        job_id = runner.start_job(ids)
    except ValueError as exc:
        return _bad(str(exc))
    return 200, {"ok": True, "job_id": job_id}


def handle_list_jobs(store: DeployStore) -> Tuple[int, Dict[str, Any]]:
    return 200, {"jobs": store.list_jobs()}


def handle_get_job(store: DeployStore, job_id: int) -> Tuple[int, Dict[str, Any]]:
    job = store.get_job(job_id)
    if not job:
        return _bad("任务不存在", 404)
    return 200, {"job": job}


def handle_install_script(
    store: DeployStore,
    token: str,
    query: str,
) -> Tuple[int, str, str]:
    """产品路径同步代码后，目标机补跑 deploy_agent.sh 用；不是第二条安装方式。"""
    qs = parse_qs(query)
    host_id = (qs.get("host_id") or ["CHANGE_ME"])[0].strip() or "CHANGE_ME"
    hostname = (qs.get("hostname") or [host_id])[0].strip()
    host_type = (qs.get("host_type") or ["auto"])[0].strip() or "auto"
    settings = store.get_settings()
    public_url = (settings.get("public_center_url") or "").strip()
    if not public_url:
        return 400, "请先配置中心对外访问地址", "text/plain; charset=utf-8"
    from center.deploy_runner import resolve_remote_dir

    remote_dir = resolve_remote_dir(
        "root",
        str(settings.get("default_remote_dir") or DEFAULT_AGENT_REMOTE_DIR),
    )
    script = build_install_script(
        public_center_url=public_url,
        host_id=host_id,
        hostname=hostname or host_id,
        host_type=host_type,
        token=token,
        interval_seconds=int(settings.get("interval_seconds") or 15),
        remote_dir=remote_dir,
    )
    return 200, script, "text/x-shellscript; charset=utf-8"


def handle_test_ssh(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    if not isinstance(data, dict):
        return _bad("根节点必须是对象")
    settings = store.get_settings(include_secrets=True)
    # 表单探测：用提交字段；也可传 target_id 用清单中的记录
    target: Dict[str, Any]
    tid = data.get("target_id")
    if tid is not None and str(tid).strip() != "":
        row = store.get_target(int(tid), include_secrets=True)
        if not row:
            return _bad("目标不存在", 404)
        target = row
        # 允许用请求体覆盖部分字段做试探
        for k in (
            "ip",
            "ssh_user",
            "ssh_port",
            "ssh_password",
            "ssh_key_path",
            "remote_dir",
        ):
            if k in data and data[k] not in (None, ""):
                target[k] = data[k]
    else:
        target = {
            "ip": data.get("ip") or "",
            "ssh_user": data.get("ssh_user") or settings.get("default_ssh_user") or "root",
            "ssh_port": data.get("ssh_port") or settings.get("default_ssh_port") or 22,
            "ssh_password": data.get("ssh_password")
            or settings.get("default_ssh_password")
            or "",
            "ssh_key_path": data.get("ssh_key_path") or settings.get("ssh_key_path") or "",
            "remote_dir": data.get("remote_dir")
            or settings.get("default_remote_dir")
            or DEFAULT_AGENT_REMOTE_DIR,
        }
    from center.deploy_runner import test_ssh_ready

    result = test_ssh_ready(target, settings)
    code = 200 if result.get("ok") else 200  # 业务失败也 200，前端看 ok
    return code, {"ok": True, "result": result}


def handle_excel_template() -> Tuple[int, bytes, str, str]:
    from center.xlsx_util import build_import_template

    data = build_import_template()
    return (
        200,
        data,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "clients-template.xlsx",
    )


def handle_import_excel(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    if not body:
        return _bad("空文件")

    settings = store.get_settings()
    defaults = {
        "ssh_user": settings.get("default_ssh_user"),
        "ssh_port": settings.get("default_ssh_port"),
        "remote_dir": settings.get("default_remote_dir"),
    }

    # xlsx: ZIP 头 PK
    if body.startswith(b"PK"):
        try:
            from center.xlsx_util import read_xlsx_rows, rows_to_target_dicts

            rows = read_xlsx_rows(body)
            items = rows_to_target_dicts(rows)
        except Exception as exc:  # noqa: BLE001
            return _bad("解析 Excel 失败: %s" % exc)
    else:
        # CSV / 文本
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = body.decode("gbk", errors="replace")
        from center.xlsx_util import rows_to_target_dicts

        rows = []
        for line in text.splitlines():
            if not line.strip() or line.strip().startswith("#"):
                continue
            if "\t" in line:
                rows.append([c.strip() for c in line.split("\t")])
            else:
                rows.append([c.strip() for c in line.split(",")])
        items = rows_to_target_dicts(rows)

    if not items:
        return _bad("未解析到有效行，请确认首行包含 ip 列（或中文「地址」）")
    n = store.import_target_dicts(items, defaults=defaults)
    # 仅对本次导入涉及的地址自动探查
    imported_keys = {
        (str(i.get("ip") or "").strip(), str(i.get("host_id") or i.get("ip") or "").strip())
        for i in items
        if (i.get("ip") or "").strip()
    }
    for t in store.list_targets():
        key = (str(t.get("ip") or "").strip(), str(t.get("host_id") or "").strip())
        if key in imported_keys:
            auto_probe_addresses(store, target=store.get_target(int(t["id"])) or t)
    return 200, {"ok": True, "imported": n, "targets": store.list_targets()}


def handle_probe_network(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    from center.netprobe import probe_host

    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")

    ports = data.get("ports") or []
    extra_ports: List[int] = []
    for p in ports:
        try:
            extra_ports.append(int(p))
        except (TypeError, ValueError):
            pass

    timeout = float(data.get("timeout") or 2.0)
    include_targets = bool(data.get("include_targets", True))
    include_peers = bool(data.get("include_peers", True))
    target_ids = data.get("target_ids")

    jobs: List[Dict[str, Any]] = []
    if include_targets:
        targets = store.list_targets()
        if isinstance(target_ids, list) and target_ids:
            idset = {int(x) for x in target_ids}
            targets = [t for t in targets if int(t["id"]) in idset]
        for t in targets:
            jobs.append(
                {
                    "source": "client",
                    "ref_id": t["id"],
                    "client_id": t["id"],
                    "client_host_id": t.get("host_id"),
                    "client_ip": t.get("ip"),
                    "name": t.get("host_id") or t.get("ip"),
                    "host": t["ip"],
                    "ssh_port": int(t.get("ssh_port") or 22),
                    "role": "client",
                }
            )
            if include_peers:
                for peer in t.get("peer_hosts") or []:
                    host = (peer.get("host") or "").strip()
                    if not host:
                        continue
                    jobs.append(
                        {
                            "source": "peer",
                            "ref_id": t["id"],
                            "client_id": t["id"],
                            "client_host_id": t.get("host_id"),
                            "client_ip": t.get("ip"),
                            "name": peer.get("name") or host,
                            "host": host,
                            "ssh_port": 22,
                            "role": "peer",
                        }
                    )

    # 同 client+host 去重
    seen = set()
    uniq = []
    for j in jobs:
        key = (j.get("client_id"), j["host"], j.get("role"))
        if key in seen:
            continue
        seen.add(key)
        uniq.append(j)

    results = []
    for j in uniq:
        probe = probe_host(
            j["host"],
            ssh_port=int(j.get("ssh_port") or 22),
            extra_ports=extra_ports,
            timeout=timeout,
        )
        probe["client_host_id"] = j.get("client_host_id")
        probe["client_ip"] = j.get("client_ip")
        probe["role"] = j.get("role")
        store.save_probe_result(
            j["host"],
            j["name"],
            "%s:%s" % (j.get("source"), j.get("client_host_id") or ""),
            probe,
        )
        if j.get("role") == "client" and j.get("ref_id") is not None:
            store.update_target_network(int(j["ref_id"]), probe)
        results.append(
            {
                "name": j["name"],
                "host": j["host"],
                "source": j["source"],
                "role": j.get("role"),
                "client_host_id": j.get("client_host_id"),
                "client_ip": j.get("client_ip"),
                "probe": probe,
            }
        )

    summary = {
        "total": len(results),
        "clients": sum(1 for r in results if r.get("role") == "client"),
        "peers": sum(1 for r in results if r.get("role") == "peer"),
        "reachable": sum(1 for r in results if r["probe"].get("reachable")),
        "unreachable": sum(1 for r in results if not r["probe"].get("reachable")),
        "ssh_ok": sum(1 for r in results if r["probe"].get("ssh_ok")),
    }
    return 200, {"ok": True, "summary": summary, "results": results}


def handle_update_target(store: DeployStore, target_id: int, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    target = store.update_target(target_id, data)
    if not target:
        return _bad("目标不存在", 404)
    probes = auto_probe_addresses(store, target=store.get_target(target_id) or target)
    target = store.get_target(target_id) or target
    return 200, {"ok": True, "target": target, "auto_probe": probes}


def handle_list_probe_results(store: DeployStore) -> Tuple[int, Dict[str, Any]]:
    return 200, {"results": store.latest_probe_results()}


def handle_add_net_host(store: DeployStore, body: bytes) -> Tuple[int, Dict[str, Any]]:
    try:
        data = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _bad("JSON 无效")
    try:
        item = store.add_net_host(
            name=data.get("name") or data.get("host") or "",
            host=data.get("host") or "",
            note=data.get("note") or "",
        )
    except ValueError as exc:
        return _bad(str(exc))
    return 200, {"ok": True, "host": item}


def handle_list_net_hosts(store: DeployStore) -> Tuple[int, Dict[str, Any]]:
    return 200, {"hosts": store.list_net_hosts()}


def handle_delete_net_host(store: DeployStore, host_id: int) -> Tuple[int, Dict[str, Any]]:
    if not store.delete_net_host(host_id):
        return _bad("不存在", 404)
    return 200, {"ok": True}
