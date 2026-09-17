"""Agent 部署失败分类与 SSH 轻量重试。

成功路径不带短码。失败统一为 ``[code] 人类可读说明``。
不对端口做扫描；SSH 仅对连接超时 / connection closed 等瞬时错误重试 2～3 次。
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

SSH_UNREACHABLE = "ssh_unreachable"
AUTH_FAIL = "auth_fail"
SSHPASS_MISSING = "sshpass_missing"
KEY_MISSING = "key_missing"
NO_PYTHON = "no_python"
SYNC_TOOL_MISSING = "sync_tool_missing"
DIR_NOT_WRITABLE = "dir_not_writable"
SUDO_REQUIRED = "sudo_required"
AGENT_START_FAIL = "agent_start_fail"
CENTER_URL_MISSING = "center_url_missing"
TIMEOUT = "timeout"
REMOTE_FAIL = "remote_fail"
UNKNOWN = "unknown"

# 含首次在内共 3 次；退避 1s、2s
SSH_ATTEMPTS = 3
SSH_BACKOFF_SEC = (1.0, 2.0)

_FAIL_LINE_RE = re.compile(
    r"\[fail\]\s*([a-z_]+)\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE
)
_CODE_PREFIX_RE = re.compile(r"^\[([a-z_]+)\]\s*(.*)$", re.IGNORECASE | re.DOTALL)

LogFn = Callable[[str], None]


class DeployError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = (code or UNKNOWN).strip() or UNKNOWN
        self.message = message or ""
        super().__init__(format_fail(self.code, self.message))


def format_fail(code: str, message: str) -> str:
    msg = (message or "").strip() or "部署失败"
    return "[%s] %s" % (code, msg)


def parse_fail_line(text: str) -> Optional[Tuple[str, str]]:
    """从脚本输出取最后一条 ``[fail] code: message``。"""
    found = None
    for match in _FAIL_LINE_RE.finditer(text or ""):
        found = (match.group(1).lower(), match.group(2).strip())
    return found


def is_retryable_ssh_error(
    text: str, exc: Optional[BaseException] = None
) -> bool:
    """是否为连接阶段瞬时失败（关闭/超时），值得再试。"""
    if isinstance(exc, subprocess.TimeoutExpired):
        # 整段部署超时（如 600s）不重试；短连接超时由输出判断
        return False
    blob = (text or "").lower()
    # scp 在 Connection refused 时也会附带 "scp: Connection closed"，不能当瞬时失败
    hard = (
        "permission denied",
        "authentication failed",
        "too many authentication",
        "connection refused",
        "no route to host",
        "network is unreachable",
        "could not resolve",
        "name or service not known",
        "no address associated",
        "host key verification failed",
    )
    if any(n in blob for n in hard):
        return False
    needles = (
        "connection timed out",
        "connection timeout",
        "operation timed out",
        "connection closed",
        "connection reset",
        "broken pipe",
        "kex_exchange_identification",
        "temporarily unavailable",
        "connection is closed",
    )
    return any(n in blob for n in needles)


def classify_deploy_failure(
    text: str,
    *,
    exc: Optional[BaseException] = None,
    default_code: str = UNKNOWN,
    default_message: str = "",
) -> Tuple[str, str]:
    """把 SSH/远端输出归一成 (code, message)。"""
    raw = text or ""
    parsed = parse_fail_line(raw)
    if parsed:
        return parsed[0], parsed[1] or default_message or parsed[0]

    prefixed = _CODE_PREFIX_RE.match(raw.strip())
    if prefixed:
        return prefixed.group(1).lower(), prefixed.group(2).strip() or default_message

    blob = raw.lower()
    if exc is not None:
        blob = (blob + "\n" + str(exc)).lower()

    def _msg(fallback: str) -> str:
        # 已命中类别时用该类说明；default_message 只用于未命中时
        return fallback

    if "ssh 私钥不存在" in blob:
        return KEY_MISSING, _msg("SSH 私钥不存在")
    if "未找到可用的 python" in blob or "no_python" in blob:
        return NO_PYTHON, _msg("目标机没有可用的 Python >= 3.6")
    if "sync_tool_missing" in blob or "rsync: command not found" in blob:
        return SYNC_TOOL_MISSING, _msg("无法同步代码（rsync/tar/cp 均不可用）")
    if "上报自检失败" in blob or "agent_start_fail" in blob:
        return AGENT_START_FAIL, _msg("Agent 上报自检或启动失败")
    if "无法创建安装目录" in blob or "安装目录不可写" in blob:
        return DIR_NOT_WRITABLE, _msg("安装目录不可写")
    if "sudo_required" in blob or "需要 sudo" in blob or "免密 sudo" in blob:
        return SUDO_REQUIRED, _msg(
            "非 root 写入 /opt 需要 sudo（sudo -n 或同一 SSH 密码 sudo -S）"
        )
    if any(
        n in blob
        for n in (
            "not in the sudoers",
            "a password is required",
            "incorrect password attempt",
            "a terminal is required to read the password",
        )
    ):
        return SUDO_REQUIRED, _msg(
            "非 root 写入 /opt 需要 sudo（sudo -n 或同一 SSH 密码 sudo -S）"
        )
    if "请先在部署设置" in blob or "public_center_url" in blob:
        return CENTER_URL_MISSING, _msg("请先填写中心对外访问地址")
    if "sshpass_missing" in blob or (
        "sshpass" in blob
        and (
            "command not found" in blob
            or "not found" in blob
            or "未安装" in blob
        )
    ):
        return SSHPASS_MISSING, _msg(
            "密码部署需要中心机安装 sshpass，或改用 SSH 密钥"
        )
    if any(
        n in blob
        for n in (
            "permission denied",
            "authentication failed",
            "too many authentication",
            "invalid user",
            "auth_fail",
        )
    ):
        return AUTH_FAIL, _msg("SSH 认证失败（用户名/密码/私钥）")
    if isinstance(exc, subprocess.TimeoutExpired):
        return TIMEOUT, _msg("部署超时")
    if any(
        n in blob
        for n in (
            "connection timed out",
            "connection timeout",
            "operation timed out",
            "connection refused",
            "connection closed",
            "connection reset",
            "no route to host",
            "network is unreachable",
            "could not resolve",
            "name or service not known",
            "no address associated",
        )
    ):
        return SSH_UNREACHABLE, _msg("SSH 不可达（检查 IP/端口/网络；默认端口 22）")
    if default_code == UNKNOWN and not default_message:
        return REMOTE_FAIL if "远端" in raw or "exit=" in blob else UNKNOWN, _msg(
            "部署失败"
        )
    return default_code, default_message or _msg("部署失败")


def run_ssh_with_retry(
    cmd: Sequence[str],
    *,
    timeout: Optional[float] = None,
    env: Optional[Dict[str, str]] = None,
    input_text: Optional[str] = None,
    attempts: int = SSH_ATTEMPTS,
    log: Optional[LogFn] = None,
    label: str = "",
) -> "subprocess.CompletedProcess[str]":
    """跑 ssh/scp；连接关闭/超时时短退避重试。"""
    last_proc: Optional[subprocess.CompletedProcess[str]] = None
    last_out = ""
    tries = max(1, int(attempts or 1))
    for i in range(tries):
        try:
            proc = subprocess.run(
                list(cmd),
                input=input_text,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            raise
        last_proc = proc
        last_out = proc.stdout or ""
        if proc.returncode == 0:
            return proc
        if i + 1 < tries and is_retryable_ssh_error(last_out):
            delay = SSH_BACKOFF_SEC[min(i, len(SSH_BACKOFF_SEC) - 1)]
            if log:
                log(
                    "%sSSH 瞬时失败，%.0fs 后重试 (%s/%s)"
                    % ((label + " ") if label else "", delay, i + 1, tries)
                )
            time.sleep(delay)
            continue
        return proc
    assert last_proc is not None
    return last_proc


def parse_inventory_line(
    line: str, default_port: int = 22
) -> Optional[Dict[str, Union[str, int]]]:
    """解析 ``IP [host_id] [ssh_port]``，IPv4 亦支持 ``IP:port``。

    不扫描端口；缺省端口为 default_port（默认 22）。
    """
    text = (line or "").split("#", 1)[0].strip()
    if not text:
        return None
    parts = text.split()
    token = parts[0]
    ip = token
    port: Optional[int] = None
    if token.count(":") == 1:
        host_part, maybe_port = token.rsplit(":", 1)
        if maybe_port.isdigit():
            value = int(maybe_port)
            if 1 <= value <= 65535:
                ip = host_part
                port = value
    host_id = ip
    if len(parts) >= 2 and parts[1]:
        host_id = parts[1]
    if len(parts) >= 3 and parts[2].isdigit():
        value = int(parts[2])
        if 1 <= value <= 65535:
            port = value
    if port is None:
        port = int(default_port or 22)
    return {"ip": ip, "host_id": host_id, "ssh_port": int(port)}
