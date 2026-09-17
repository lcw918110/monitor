"""通过 SSH 在目标机执行 Agent 自动部署。

认证优先级（每台客户端独立解析）：
1. 该客户端的 ssh_password（或全局默认密码）→ 用户名密码
2. 否则该客户端的 ssh_key_path（或全局 ssh_key_path）→ 指定私钥
3. 否则使用本机默认密钥（~/.ssh/id_rsa 等），BatchMode 免交互

说明：全局「SSH 私钥路径」不是按 IP 一对一映射，而是所有未单独指定密钥的客户端共用；
若某台机要用不同密钥/密码，在该客户端上单独填写。
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

from center.deploy_errors import (
    AGENT_START_FAIL,
    AUTH_FAIL,
    CENTER_URL_MISSING,
    DIR_NOT_WRITABLE,
    KEY_MISSING,
    NO_PYTHON,
    REMOTE_FAIL,
    SSH_UNREACHABLE,
    TIMEOUT,
    UNKNOWN,
    DeployError,
    classify_deploy_failure,
    format_fail,
    run_ssh_with_retry,
)
from center.deploy_store import DeployStore


LogFn = Callable[[str], None]


def _project_root() -> str:
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def build_install_script(
    *,
    public_center_url: str,
    host_id: str,
    hostname: str,
    host_type: str,
    token: str,
    interval_seconds: int,
    remote_dir: str,
) -> str:
    """生成可下载的通用安装脚本。

    注意：此处 INSTALL_DIR 仅在本独立脚本内赋值使用，不会嵌套进带 set -u 的外层脚本。
    在线 SSH 部署路径见 deploy_one_target（使用 REMOTE_DIR，不经过本函数）。
    """
    center = public_center_url.rstrip("/")
    rd = (remote_dir or "/opt/monitor").strip() or "/opt/monitor"
    if rd.startswith("~"):
        install_assign = 'INSTALL_DIR="$HOME%s"' % rd[1:]
    else:
        install_assign = "INSTALL_DIR=%s" % shlex.quote(rd)
    return f"""#!/usr/bin/env bash
set -euo pipefail
# 由中心端页面生成的客户端安装脚本
CENTER_URL={shlex.quote(center)}
HOST_ID={shlex.quote(host_id)}
HOSTNAME_CFG={shlex.quote(hostname or host_id)}
HOST_TYPE={shlex.quote(host_type or 'auto')}
TOKEN={shlex.quote(token or '')}
INTERVAL={int(interval_seconds or 15)}
{install_assign}

if [[ ! -d "$INSTALL_DIR/agent" ]]; then
  echo "未找到监控代码目录: $INSTALL_DIR"
  echo "请先由中心端 SSH 自动部署，或手动把代码同步到该目录后再执行。"
  exit 1
fi
cd "$INSTALL_DIR"
chmod +x scripts/*.sh 2>/dev/null || true
./scripts/deploy_agent.sh \\
  --center-url "$CENTER_URL" \\
  --dir "$INSTALL_DIR" \\
  --host-id "$HOST_ID" \\
  --hostname "$HOSTNAME_CFG" \\
  --host-type "$HOST_TYPE" \\
  --token "$TOKEN" \\
  --interval "$INTERVAL"
"""


def _make_package_tgz(root: str) -> str:
    fd, path = tempfile.mkstemp(prefix="monitor-agent-", suffix=".tgz")
    os.close(fd)
    with tarfile.open(path, "w:gz") as tar:
        for name in ("agent", "center", "common", "config", "scripts", "README.md"):
            full = os.path.join(root, name)
            if os.path.exists(full):
                tar.add(full, arcname=name)
    return path


def resolve_ssh_auth(
    target: Dict[str, Any], settings: Dict[str, Any]
) -> Dict[str, Any]:
    """解析某台客户端最终使用的认证方式。"""
    password = (
        str(target.get("ssh_password") or "").strip()
        or str(settings.get("default_ssh_password") or "").strip()
    )
    key_raw = (
        str(target.get("ssh_key_path") or "").strip()
        or str(settings.get("ssh_key_path") or "").strip()
    )
    key_path = os.path.expanduser(key_raw) if key_raw else ""
    if key_path and not os.path.isfile(key_path):
        # 路径填了但不存在：留给调用方报错更清晰
        pass
    if password:
        return {"mode": "password", "password": password, "key_path": key_path or ""}
    if key_path:
        return {"mode": "key", "password": "", "key_path": key_path}
    return {"mode": "default_key", "password": "", "key_path": ""}


def _build_askpass(password: str) -> str:
    fd, path = tempfile.mkstemp(prefix="monitor-askpass-", suffix=".sh")
    os.close(fd)
    # 用 printf 避免 echo 对 -n 等参数误判
    body = "#!/bin/sh\nprintf '%%s\\n' %s\n" % shlex.quote(password)
    with open(path, "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return path


def build_ssh_scp_cmds(
    *,
    ssh_port: int,
    auth: Dict[str, Any],
) -> Tuple[List[str], List[str], Dict[str, str], Optional[str]]:
    """构造 ssh/scp 命令与环境变量；返回 (ssh_cmd, scp_cmd, env, askpass_path_to_cleanup)。"""
    common = [
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=12",
    ]
    mode = auth.get("mode") or "default_key"
    key_path = auth.get("key_path") or ""
    password = auth.get("password") or ""
    cleanup: Optional[str] = None
    env = dict(os.environ)

    if mode == "password" and password:
        common.extend(
            [
                "-o",
                "PreferredAuthentications=password,keyboard-interactive",
                "-o",
                "PubkeyAuthentication=no",
                "-o",
                "NumberOfPasswordPrompts=1",
            ]
        )
        ssh_cmd = ["ssh", "-p", str(ssh_port)] + common
        scp_cmd = ["scp", "-P", str(ssh_port)] + common
        if shutil.which("sshpass"):
            ssh_cmd = ["sshpass", "-p", password] + ssh_cmd
            scp_cmd = ["sshpass", "-p", password] + scp_cmd
        else:
            cleanup = _build_askpass(password)
            env["SSH_ASKPASS"] = cleanup
            env["SSH_ASKPASS_REQUIRE"] = "force"
            env["DISPLAY"] = env.get("DISPLAY") or ":0"
            # 无 TTY 时才会走 ASKPASS
            ssh_cmd = ["ssh", "-p", str(ssh_port)] + common
            scp_cmd = ["scp", "-P", str(ssh_port)] + common
        return ssh_cmd, scp_cmd, env, cleanup

    # 密钥 / 默认密钥：禁止交互卡住
    common.extend(["-o", "BatchMode=yes"])
    ssh_cmd = ["ssh", "-p", str(ssh_port)] + common
    scp_cmd = ["scp", "-P", str(ssh_port)] + common
    if mode == "key" and key_path:
        ssh_cmd.extend(["-i", key_path, "-o", "IdentitiesOnly=yes"])
        scp_cmd.extend(["-i", key_path, "-o", "IdentitiesOnly=yes"])
    return ssh_cmd, scp_cmd, env, cleanup


def resolve_remote_dir(ssh_user: str, remote_dir: str) -> str:
    """非 root 默认不要用 /opt/monitor（通常无写权限），改为 ~/monitor。"""
    user = (ssh_user or "root").strip() or "root"
    rd = (remote_dir or "").strip() or "/opt/monitor"
    if user != "root" and rd.rstrip("/") == "/opt/monitor":
        return "~/monitor"
    return rd


def test_ssh_ready(
    target: Dict[str, Any],
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    """探测：SSH 可达 + 安装目录可写（部署前置条件）。"""
    ip = (target.get("ip") or "").strip()
    if not ip:
        return {"ok": False, "ssh_ok": False, "writable": False, "message": "请填写 IP"}
    ssh_user = target.get("ssh_user") or settings.get("default_ssh_user") or "root"
    ssh_port = int(target.get("ssh_port") or settings.get("default_ssh_port") or 22)
    remote_dir = resolve_remote_dir(
        ssh_user,
        target.get("remote_dir") or settings.get("default_remote_dir") or "/opt/monitor",
    )
    auth = resolve_ssh_auth(target, settings)
    if auth["mode"] == "key" and auth["key_path"] and not os.path.isfile(auth["key_path"]):
        return {
            "ok": False,
            "ssh_ok": False,
            "writable": False,
            "error_code": KEY_MISSING,
            "message": format_fail(KEY_MISSING, "SSH 私钥不存在: %s" % auth["key_path"]),
            "remote_dir": remote_dir,
        }
    ssh_base, _, env, askpass_path = build_ssh_scp_cmds(ssh_port=ssh_port, auth=auth)
    remote = "%s@%s" % (ssh_user, ip)
    script = f"""set -euo pipefail
echo SSH_OK
REMOTE_DIR=$(eval echo {shlex.quote(remote_dir)})
mkdir -p "$REMOTE_DIR"
TESTFILE="$REMOTE_DIR/.monitor_write_test.$$"
echo ok > "$TESTFILE"
rm -f "$TESTFILE"
echo WRITE_OK
echo REMOTE_DIR=$REMOTE_DIR
"""
    try:
        proc = run_ssh_with_retry(
            ssh_base + [remote, "bash", "-s"],
            input_text=script,
            timeout=20,
            env=env,
        )
        out = (proc.stdout or "").strip()
        ssh_ok = "SSH_OK" in out
        writable = "WRITE_OK" in out
        resolved = remote_dir
        for line in out.splitlines():
            if line.startswith("REMOTE_DIR="):
                resolved = line.split("=", 1)[1].strip() or resolved
        if proc.returncode == 0 and ssh_ok and writable:
            return {
                "ok": True,
                "ssh_ok": True,
                "writable": True,
                "error_code": "",
                "message": "连通正常，具备部署条件（目录 %s 可写）" % resolved,
                "remote_dir": resolved,
                "detail": out,
            }
        if not ssh_ok:
            code, human = classify_deploy_failure(
                out,
                default_code=SSH_UNREACHABLE,
                default_message="SSH 登录失败（检查用户名/密码/私钥/网络/端口）",
            )
            if code in (REMOTE_FAIL, UNKNOWN) and "permission denied" in out.lower():
                code = AUTH_FAIL
        else:
            code = DIR_NOT_WRITABLE
            human = "SSH 已通，但安装目录不可写：%s（可改为 ~/monitor）" % remote_dir
        if out and "输出:" not in human:
            human = human + "；输出: " + " | ".join(out.splitlines()[-5:])
        return {
            "ok": False,
            "ssh_ok": ssh_ok,
            "writable": writable,
            "error_code": code,
            "message": format_fail(code, human),
            "remote_dir": resolved,
            "detail": out,
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "ssh_ok": False,
            "writable": False,
            "error_code": TIMEOUT,
            "message": format_fail(TIMEOUT, "连接超时（20s）"),
            "remote_dir": remote_dir,
        }
    except Exception as exc:  # noqa: BLE001
        code, human = classify_deploy_failure(str(exc), exc=exc)
        return {
            "ok": False,
            "ssh_ok": False,
            "writable": False,
            "error_code": code,
            "message": format_fail(code, "探测异常: %s" % human),
            "remote_dir": remote_dir,
        }
    finally:
        if askpass_path:
            try:
                os.remove(askpass_path)
            except OSError:
                pass


def deploy_one_target(
    target: Dict[str, Any],
    settings: Dict[str, Any],
    token: str,
    log: LogFn,
) -> None:
    root = _project_root()
    public_url = (settings.get("public_center_url") or "").strip()
    if not public_url:
        raise DeployError(CENTER_URL_MISSING, "请先在部署设置中填写「中心对外访问地址」public_center_url")

    ip = target["ip"]
    ssh_user = target.get("ssh_user") or settings.get("default_ssh_user") or "root"
    ssh_port = int(target.get("ssh_port") or settings.get("default_ssh_port") or 22)
    remote_dir = resolve_remote_dir(
        ssh_user,
        target.get("remote_dir") or settings.get("default_remote_dir") or "/opt/monitor",
    )
    host_id = target.get("host_id") or ip
    hostname = target.get("hostname") or host_id
    host_type = target.get("host_type") or "auto"
    interval = int(settings.get("interval_seconds") or 15)

    auth = resolve_ssh_auth(target, settings)
    if auth["mode"] == "key" and auth["key_path"] and not os.path.isfile(auth["key_path"]):
        raise DeployError(KEY_MISSING, "SSH 私钥不存在: %s（客户端 %s）" % (auth["key_path"], ip))

    ssh_base, scp_base, env, askpass_path = build_ssh_scp_cmds(
        ssh_port=ssh_port, auth=auth
    )
    mode_label = {
        "password": "用户名密码",
        "key": "指定私钥",
        "default_key": "本机默认私钥",
    }.get(auth["mode"], auth["mode"])
    log("[%s] 认证方式: %s  用户=%s 端口=%s" % (ip, mode_label, ssh_user, ssh_port))
    log("[%s] 安装目录: %s" % (ip, remote_dir))
    if auth["mode"] == "key":
        log("[%s] 私钥: %s" % (ip, auth["key_path"]))

    log("[%s] 打包代码..." % ip)
    tgz = _make_package_tgz(root)
    remote = "%s@%s" % (ssh_user, ip)
    try:
        log("[%s] 上传安装包..." % ip)
        try:
            proc = run_ssh_with_retry(
                scp_base + [tgz, "%s:/tmp/monitor-agent.tgz" % remote],
                timeout=60,
                env=env,
                log=log,
                label="[%s]" % ip,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployError(TIMEOUT, "SCP 超时") from exc
        if proc.returncode != 0:
            code, human = classify_deploy_failure(
                proc.stdout or "",
                default_code=SSH_UNREACHABLE,
                default_message="SCP 失败（请检查用户名/密码/私钥/网络/端口）。exit=%s"
                % proc.returncode,
            )
            raise DeployError(code, human)
        # 远端展开 ~ 后直接安装，避免嵌套 heredoc 在 set -u 下误展开变量
        center = public_url.rstrip("/")
        remote_script = f"""set -euo pipefail
REMOTE_DIR=$(eval echo {shlex.quote(remote_dir)})
echo "REMOTE_DIR=$REMOTE_DIR"
mkdir -p "$REMOTE_DIR" || {{
  echo "无法创建安装目录: $REMOTE_DIR"
  echo "非 root 用户请使用 ~/monitor，或在清单中修改「远端安装目录」"
  exit 1
}}
tar -xzf /tmp/monitor-agent.tgz -C "$REMOTE_DIR"
cd "$REMOTE_DIR"
chmod +x scripts/*.sh 2>/dev/null || true
./scripts/deploy_agent.sh \\
  --center-url {shlex.quote(center)} \\
  --dir "$REMOTE_DIR" \\
  --host-id {shlex.quote(host_id)} \\
  --hostname {shlex.quote(hostname or host_id)} \\
  --host-type {shlex.quote(host_type or "auto")} \\
  --token {shlex.quote(token or "")} \\
  --interval {int(interval or 15)}
rm -f /tmp/monitor-agent.tgz
echo DEPLOY_DONE
"""
        log("[%s] 远端安装中..." % ip)
        try:
            proc = run_ssh_with_retry(
                ssh_base + [remote, "bash", "-s"],
                input_text=remote_script,
                timeout=600,
                env=env,
                log=log,
                label="[%s]" % ip,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployError(TIMEOUT, "远端安装超时（600s）") from exc
        out = proc.stdout or ""
        for line in out.splitlines()[-40:]:
            log("[%s] %s" % (ip, line))
        if "DEPLOY_DONE" in out:
            log("[%s] 部署成功" % ip)
            return
        if proc.returncode != 0:
            hint = ""
            if "Permission denied" in out:
                hint = "（权限不足：非 root 已自动改用 ~/monitor，若仍失败请检查家目录权限）"
            elif "unbound variable" in out:
                hint = "（远端脚本变量错误，请更新中心端后重试）"
            code, human = classify_deploy_failure(
                out,
                default_code=REMOTE_FAIL,
                default_message="远端安装失败，exit=%s%s" % (proc.returncode, hint),
            )
            if "未找到可用的 Python" in out:
                code, human = NO_PYTHON, "目标机没有可用的 Python >= 3.6"
            elif "上报自检失败" in out:
                code, human = AGENT_START_FAIL, "Agent 上报自检失败（检查中心地址/Token/网络）"
            raise DeployError(code, human)
        log("[%s] 部署成功" % ip)
    finally:
        try:
            os.remove(tgz)
        except OSError:
            pass
        if askpass_path:
            try:
                os.remove(askpass_path)
            except OSError:
                pass


class DeployRunner:
    def __init__(self, store: DeployStore, token: str = "") -> None:
        self.store = store
        self.token = token or ""
        self._lock = threading.Lock()

    def start_job(self, target_ids: Optional[List[int]] = None) -> int:
        with self._lock:
            targets = self.store.list_targets(include_secrets=True)
            if target_ids:
                idset = set(int(x) for x in target_ids)
                targets = [t for t in targets if int(t["id"]) in idset]
            if not targets:
                raise ValueError("没有可部署的目标")
            settings = self.store.get_settings(include_secrets=True)
            if not (settings.get("public_center_url") or "").strip():
                raise ValueError("请先保存「中心对外访问地址」")
            job_id = self.store.create_job([int(t["id"]) for t in targets])
            thread = threading.Thread(
                target=self._run_job,
                args=(job_id, targets, settings),
                daemon=True,
                name="deploy-job-%s" % job_id,
            )
            thread.start()
            return job_id

    def _run_job(
        self,
        job_id: int,
        targets: List[Dict[str, Any]],
        settings: Dict[str, Any],
    ) -> None:
        def log(line: str) -> None:
            self.store.append_job_log(job_id, line + "\n")

        ok_all = True
        log("开始部署，共 %s 台" % len(targets))
        for t in targets:
            tid = int(t["id"])
            self.store.update_target_status(tid, "deploying", "部署中")
            try:
                deploy_one_target(t, settings, self.token, log)
                self.store.update_target_status(tid, "success", "部署成功", error_code="")
            except DeployError as exc:
                ok_all = False
                log("[%s] 失败: %s" % (t.get("ip"), str(exc)))
                self.store.update_target_status(
                    tid, "failed", str(exc), error_code=exc.code
                )
            except Exception as exc:  # noqa: BLE001
                ok_all = False
                code, human = classify_deploy_failure(str(exc), exc=exc)
                msg = format_fail(code, human)
                log("[%s] 失败: %s" % (t.get("ip"), msg))
                self.store.update_target_status(tid, "failed", msg, error_code=code)
        self.store.finish_job(job_id, "success" if ok_all else "failed")
        log("任务结束: %s" % ("全部成功" if ok_all else "存在失败"))
