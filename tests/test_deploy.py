#!/usr/bin/env python3
"""部署模块基础测试。"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.deploy_api import handle_add_target, handle_import_targets, handle_save_settings
from center.deploy_errors import (
    AGENT_START_FAIL,
    AUTH_FAIL,
    NO_PYTHON,
    SSHPASS_MISSING,
    SSH_UNREACHABLE,
    SUDO_REQUIRED,
    SYNC_TOOL_MISSING,
    classify_deploy_failure,
    is_retryable_ssh_error,
    parse_fail_line,
    parse_inventory_line,
    run_ssh_with_retry,
)
from center.deploy_runner import (
    build_remote_root_helper,
    build_ssh_scp_cmds,
    needs_remote_sudo,
    require_sshpass_for_password,
    resolve_remote_dir,
    test_ssh_ready,
)
from center.deploy_store import DEFAULT_AGENT_REMOTE_DIR, DeployStore


class DeployStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DeployStore(os.path.join(self.tmp.name, "d.db"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_settings_and_targets(self) -> None:
        code, resp = handle_save_settings(
            self.store,
            b'{"public_center_url":"http://10.0.0.8:8080","default_ssh_user":"ubuntu"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["settings"]["public_center_url"], "http://10.0.0.8:8080")

        code, resp = handle_add_target(
            self.store,
            b'{"ip":"10.0.0.21","host_id":"npu-21","host_type":"npu"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["target"]["host_id"], "npu-21")
        self.assertEqual(resp["target"]["host_type"], "gpu")  # 旧 npu → gpu
        self.assertEqual(int(resp["target"]["ssh_port"]), 22)
        self.assertEqual(resp["target"]["remote_dir"], DEFAULT_AGENT_REMOTE_DIR)

        code, resp = handle_import_targets(
            self.store,
            b'{"text":"10.0.0.22 npu-22\\n10.0.0.23\\n"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["imported"], 2)
        self.assertEqual(len(self.store.list_targets()), 3)

    def test_optional_ssh_port_in_inventory(self) -> None:
        code, resp = handle_import_targets(
            self.store,
            b'{"text":"10.0.0.24 gpu-24 2222\\n10.0.0.25:2200 gpu-25\\n10.0.0.26\\n"}',
        )
        self.assertEqual(code, 200)
        self.assertEqual(resp["imported"], 3)
        by_ip = {t["ip"]: t for t in self.store.list_targets()}
        self.assertEqual(int(by_ip["10.0.0.24"]["ssh_port"]), 2222)
        self.assertEqual(by_ip["10.0.0.24"]["host_id"], "gpu-24")
        self.assertEqual(int(by_ip["10.0.0.25"]["ssh_port"]), 2200)
        self.assertEqual(int(by_ip["10.0.0.26"]["ssh_port"]), 22)

    def test_saved_legacy_remote_dir_not_migrated(self) -> None:
        self.store.update_settings({"default_remote_dir": "/opt/monitor"})
        settings = self.store.get_settings()
        self.assertEqual(settings["default_remote_dir"], "/opt/monitor")
        t = self.store.add_target(
            {"ip": "10.0.0.31", "host_id": "legacy", "remote_dir": "/opt/monitor"}
        )
        self.assertEqual(t["remote_dir"], "/opt/monitor")

    def test_last_error_code_cleared_on_success(self) -> None:
        t = self.store.add_target({"ip": "10.0.0.9", "host_id": "h9"})
        tid = int(t["id"])
        self.store.update_target_status(
            tid, "failed", "[ssh_unreachable] 连不上", error_code="ssh_unreachable"
        )
        row = self.store.get_target(tid)
        self.assertEqual(row["last_error_code"], "ssh_unreachable")
        self.store.update_target_status(tid, "success", "部署成功")
        row = self.store.get_target(tid)
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["last_message"], "部署成功")
        self.assertEqual(row["last_error_code"], "")


class InventoryParseTests(unittest.TestCase):
    def test_parse_variants(self) -> None:
        self.assertEqual(
            parse_inventory_line("10.0.0.1"),
            {"ip": "10.0.0.1", "host_id": "10.0.0.1", "ssh_port": 22},
        )
        self.assertEqual(
            parse_inventory_line("10.0.0.1 node-a"),
            {"ip": "10.0.0.1", "host_id": "node-a", "ssh_port": 22},
        )
        self.assertEqual(
            parse_inventory_line("10.0.0.1 node-a 2222"),
            {"ip": "10.0.0.1", "host_id": "node-a", "ssh_port": 2222},
        )
        self.assertEqual(
            parse_inventory_line("10.0.0.1:2222 node-a"),
            {"ip": "10.0.0.1", "host_id": "node-a", "ssh_port": 2222},
        )
        self.assertIsNone(parse_inventory_line("# comment"))
        self.assertEqual(parse_inventory_line("10.0.0.8", default_port=2200)["ssh_port"], 2200)


class ClassifyTests(unittest.TestCase):
    def test_fail_line_from_agent_script(self) -> None:
        text = "foo\n[fail] no_python: 未找到可用的 Python >= 3.6\n"
        self.assertEqual(parse_fail_line(text), (NO_PYTHON, "未找到可用的 Python >= 3.6"))
        code, msg = classify_deploy_failure(text)
        self.assertEqual(code, NO_PYTHON)
        self.assertIn("Python", msg)

    def test_categories(self) -> None:
        cases = [
            ("Permission denied (publickey,password).", AUTH_FAIL),
            ("ssh: connect to host 10.0.0.1 port 22: Connection timed out", SSH_UNREACHABLE),
            ("ssh: connect to host 10.0.0.1 port 2222: Connection refused", SSH_UNREACHABLE),
            ("kex_exchange_identification: Connection closed by remote host", SSH_UNREACHABLE),
            ("上报自检失败：请检查中心地址/Token/网络", AGENT_START_FAIL),
            ("[fail] sync_tool_missing: 无法同步代码", SYNC_TOOL_MISSING),
            ("sshpass: command not found", SSHPASS_MISSING),
            ("[sshpass_missing] 密码部署需要中心机安装 sshpass", SSHPASS_MISSING),
            ("[fail] sudo_required: 非 root 无法写入 /opt", SUDO_REQUIRED),
        ]
        for text, expected in cases:
            code, _msg = classify_deploy_failure(text)
            self.assertEqual(code, expected, text)

    def test_retryable_only_transient_connect(self) -> None:
        self.assertTrue(is_retryable_ssh_error("Connection closed by remote host"))
        self.assertTrue(is_retryable_ssh_error("Connection timed out"))
        self.assertFalse(is_retryable_ssh_error("Permission denied"))
        self.assertFalse(is_retryable_ssh_error("Connection refused"))
        scp_refused = (
            "ssh: connect to host 127.0.0.1 port 2222: Connection refused\n"
            "scp: Connection closed\n"
        )
        self.assertFalse(is_retryable_ssh_error(scp_refused))
        code, msg = classify_deploy_failure(
            scp_refused, default_message="SCP 失败 exit=255"
        )
        self.assertEqual(code, SSH_UNREACHABLE)
        self.assertIn("不可达", msg)
        self.assertNotIn("SCP 失败", msg)

    def test_ssh_retry_then_success(self) -> None:
        calls = {"n": 0}

        def fake_run(*_a, **_k):
            calls["n"] += 1
            if calls["n"] < 3:
                return subprocess.CompletedProcess(
                    args=["ssh"],
                    returncode=255,
                    stdout="kex_exchange_identification: Connection closed by remote host\n",
                )
            return subprocess.CompletedProcess(args=["ssh"], returncode=0, stdout="ok\n")

        with mock.patch("center.deploy_errors.time.sleep", return_value=None):
            with mock.patch("center.deploy_errors.subprocess.run", side_effect=fake_run):
                proc = run_ssh_with_retry(["ssh", "x"], timeout=5)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(calls["n"], 3)

    def test_ssh_retry_gives_up(self) -> None:
        def always_closed(*_a, **_k):
            return subprocess.CompletedProcess(
                args=["ssh"],
                returncode=255,
                stdout="Connection timed out\n",
            )

        with mock.patch("center.deploy_errors.time.sleep", return_value=None):
            with mock.patch("center.deploy_errors.subprocess.run", side_effect=always_closed):
                proc = run_ssh_with_retry(["ssh", "x"], attempts=3)
        self.assertEqual(proc.returncode, 255)


class SyncTreeTests(unittest.TestCase):
    def _run_sync(self, src: str, dst: str, extra_env=None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        if extra_env:
            env.update(extra_env)
        script = textwrap.dedent(
            """
            set -euo pipefail
            . "{root}/scripts/lib/sync_tree.sh"
            sync_tree "{src}" "{dst}"
            """
        ).format(root=ROOT, src=src, dst=dst)
        return subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

    def _make_tree(self) -> str:
        src = tempfile.mkdtemp(prefix="sync-src-")
        os.makedirs(os.path.join(src, "agent"))
        os.makedirs(os.path.join(src, "config"))
        with open(os.path.join(src, "agent", "ok.txt"), "w", encoding="utf-8") as f:
            f.write("hello")
        with open(os.path.join(src, "config", "keep.json"), "w", encoding="utf-8") as f:
            f.write("{}")
        with open(os.path.join(src, "config", "agent.json"), "w", encoding="utf-8") as f:
            f.write('{"old":1}')
        os.makedirs(os.path.join(src, "data"))
        with open(os.path.join(src, "data", "x.db"), "w", encoding="utf-8") as f:
            f.write("db")
        return src

    def test_rsync_or_fallback_copies_and_skips_agent_json(self) -> None:
        src = self._make_tree()
        dst = tempfile.mkdtemp(prefix="sync-dst-")
        os.makedirs(os.path.join(dst, "config"))
        with open(os.path.join(dst, "config", "agent.json"), "w", encoding="utf-8") as f:
            f.write('{"keep":true}')
        try:
            proc = self._run_sync(src, dst)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("[sync] method=", proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dst, "agent", "ok.txt")))
            with open(os.path.join(dst, "config", "agent.json"), encoding="utf-8") as f:
                self.assertIn("keep", f.read())
            self.assertFalse(os.path.isdir(os.path.join(dst, "data")))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_tar_fallback_when_rsync_disabled(self) -> None:
        src = self._make_tree()
        dst = tempfile.mkdtemp(prefix="sync-dst-")
        try:
            proc = self._run_sync(src, dst, extra_env={"MONITOR_SYNC_DISABLE_RSYNC": "1"})
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("method=tar", proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dst, "agent", "ok.txt")))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_cp_fallback_when_rsync_and_tar_disabled(self) -> None:
        src = self._make_tree()
        dst = tempfile.mkdtemp(prefix="sync-dst-")
        try:
            proc = self._run_sync(
                src,
                dst,
                extra_env={
                    "MONITOR_SYNC_DISABLE_RSYNC": "1",
                    "MONITOR_SYNC_DISABLE_TAR": "1",
                },
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("method=cp", proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dst, "agent", "ok.txt")))
            self.assertTrue(os.path.isfile(os.path.join(dst, "config", "keep.json")))
            self.assertFalse(os.path.isfile(os.path.join(dst, "config", "agent.json")))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_inplace_when_src_is_dst(self) -> None:
        src = self._make_tree()
        try:
            proc = self._run_sync(
                src, src, extra_env={"MONITOR_SYNC_DISABLE_RSYNC": "1"}
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("method=inplace", proc.stdout)
        finally:
            shutil.rmtree(src, ignore_errors=True)


class FleetScriptTests(unittest.TestCase):
    def test_flags_and_lib(self) -> None:
        path = os.path.join(ROOT, "scripts", "deploy_fleet.sh")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        self.assertIn("--ssh-port|--port", src)
        self.assertIn("deploy_ssh.sh", src)
        self.assertIn("parse_inventory_line", src)
        agent = os.path.join(ROOT, "scripts", "deploy_agent.sh")
        with open(agent, encoding="utf-8") as f:
            asrc = f.read()
        self.assertIn("sync_tree.sh", asrc)
        self.assertIn("fail_deploy", asrc)
        self.assertIn("/opt/monitor-agent", asrc)
        self.assertNotIn("rsync -a --delete", asrc)
        self.assertIn("REMOTE_DIR=\"/opt/monitor-agent\"", src)

    def test_bash_inventory_parse(self) -> None:
        script = textwrap.dedent(
            """
            set -euo pipefail
            . "{root}/scripts/lib/deploy_ssh.sh"
            parse_inventory_line "10.1.2.3 gpu-x 2222"
            echo "$HOST_IP $HOST_ID $LINE_SSH_PORT"
            parse_inventory_line "10.1.2.4:2200 hid"
            echo "$HOST_IP $HOST_ID $LINE_SSH_PORT"
            if ssh_is_retryable $'ssh: connect to host x port 2222: Connection refused\\nscp: Connection closed\\n'; then
              echo RETRY_BAD
              exit 1
            fi
            echo REFUSED_NO_RETRY
            if ssh_is_retryable "Connection timed out"; then
              echo TIMEOUT_RETRY
            fi
            """
        ).format(root=ROOT)
        proc = subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        self.assertEqual(lines[0].split(), ["10.1.2.3", "gpu-x", "2222"])
        self.assertEqual(lines[1].split(), ["10.1.2.4", "hid", "2200"])
        self.assertIn("REFUSED_NO_RETRY", proc.stdout)
        self.assertIn("TIMEOUT_RETRY", proc.stdout)


class RemoteDirAndSshpassTests(unittest.TestCase):
    def test_resolve_remote_dir(self) -> None:
        self.assertEqual(resolve_remote_dir("root", ""), DEFAULT_AGENT_REMOTE_DIR)
        self.assertEqual(resolve_remote_dir("root", "/opt/monitor"), "/opt/monitor")
        self.assertEqual(
            resolve_remote_dir("jykj", "/opt/monitor-agent"), "/opt/monitor-agent"
        )
        self.assertEqual(resolve_remote_dir("ubuntu", "/opt/monitor"), "/opt/monitor")
        self.assertEqual(resolve_remote_dir("ubuntu", "~/monitor-agent"), "~/monitor-agent")
        self.assertEqual(resolve_remote_dir("ubuntu", "/data/agent"), "/data/agent")

    def test_nonroot_opt_uses_sudo_helper(self) -> None:
        self.assertTrue(needs_remote_sudo("jykj", "/opt/monitor-agent"))
        self.assertTrue(needs_remote_sudo("ubuntu", "/opt/monitor"))
        self.assertFalse(needs_remote_sudo("root", "/opt/monitor-agent"))
        self.assertFalse(needs_remote_sudo("jykj", "~/monitor-agent"))
        auth = {"mode": "password", "password": "pw-of-jykj", "key_path": ""}
        helper = build_remote_root_helper(auth, use_sudo=True)
        self.assertIn("sudo -n", helper)
        self.assertIn("sudo -S", helper)
        self.assertIn("sudo_required", helper)
        self.assertIn("pw-of-jykj", helper)
        nosudo = build_remote_root_helper(auth, use_sudo=False)
        self.assertIn('MONITOR_SUDO_PW=""', nosudo)
        self.assertNotIn("pw-of-jykj", nosudo)

    def test_password_without_sshpass_is_sshpass_missing(self) -> None:
        auth = {"mode": "password", "password": "secret", "key_path": ""}
        with mock.patch("center.deploy_runner.shutil.which", return_value=None):
            with self.assertRaises(Exception) as ctx:
                require_sshpass_for_password(auth)
            self.assertEqual(ctx.exception.code, SSHPASS_MISSING)
            with self.assertRaises(Exception):
                build_ssh_scp_cmds(ssh_port=22, auth=auth)
            result = test_ssh_ready(
                {
                    "ip": "10.0.0.9",
                    "ssh_user": "root",
                    "ssh_password": "secret",
                    "remote_dir": DEFAULT_AGENT_REMOTE_DIR,
                },
                {"default_ssh_user": "root", "default_ssh_port": 22},
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], SSHPASS_MISSING)
        self.assertIn("sshpass", result["message"])

    def test_password_with_sshpass_builds_cmd(self) -> None:
        auth = {"mode": "password", "password": "secret", "key_path": ""}
        with mock.patch("center.deploy_runner.shutil.which", return_value="/usr/bin/sshpass"):
            ssh_cmd, scp_cmd, _env, cleanup = build_ssh_scp_cmds(ssh_port=2222, auth=auth)
        self.assertEqual(ssh_cmd[0], "sshpass")
        self.assertIn("-p", ssh_cmd)
        self.assertIn("2222", ssh_cmd)
        self.assertEqual(scp_cmd[0], "sshpass")
        self.assertIsNone(cleanup)


if __name__ == "__main__":
    unittest.main()
