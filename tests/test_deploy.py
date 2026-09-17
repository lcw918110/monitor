#!/usr/bin/env python3
"""部署模块基础测试。"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
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
    DeployError,
    NO_PYTHON,
    PACKAGE_INCOMPLETE,
    REMOTE_FAIL,
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
    OPTIONAL_PACKAGE_PARTS,
    REQUIRED_PACKAGE_PARTS,
    _make_package_tgz,
    build_remote_root_helper,
    build_ssh_scp_cmds,
    build_ssh_upload_cmd,
    missing_package_parts,
    needs_remote_sudo,
    require_sshpass_for_password,
    resolve_remote_dir,
    test_ssh_ready,
    upload_file_via_ssh,
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
            ("sudo: a password is required", SUDO_REQUIRED),
            ("jykj is not in the sudoers file", SUDO_REQUIRED),
            ("[fail] package_incomplete: 安装包不含 agent/", PACKAGE_INCOMPLETE),
            ("/usr/bin/python3.10: No module named agent", PACKAGE_INCOMPLETE),
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

    def test_scp_missing_remote_not_classified_unreachable(self) -> None:
        """历史上 scp 失败默认 ssh_unreachable；管道上传失败应保持 remote_fail。"""
        text = (
            "SCP 失败 ... exit=1\n"
            "bash: scp: command not found\n"
            "lost connection\n"
        )
        code, msg = classify_deploy_failure(
            text, default_code=REMOTE_FAIL, default_message="上传失败 exit=1"
        )
        self.assertEqual(code, REMOTE_FAIL)
        self.assertNotEqual(code, SSH_UNREACHABLE)
        self.assertIn("上传失败", msg)

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

    def test_stdin_path_retry_rereads_binary_file(self) -> None:
        fd, path = tempfile.mkstemp(prefix="ssh-stdin-")
        os.close(fd)
        payload = b"abc\x00def\x1f\x8b\r\n"
        calls = {"n": 0, "data": []}  # type: ignore[var-annotated]
        try:
            with open(path, "wb") as f:
                f.write(payload)

            def fake_run(_cmd, **kwargs):
                calls["n"] += 1
                stdin = kwargs.get("stdin")
                calls["data"].append(stdin.read())
                if calls["n"] == 1:
                    return subprocess.CompletedProcess(
                        args=["ssh"],
                        returncode=255,
                        stdout=b"Connection reset by peer\n",
                    )
                return subprocess.CompletedProcess(
                    args=["ssh"], returncode=0, stdout=b"ok\n"
                )

            with mock.patch("center.deploy_errors.time.sleep", return_value=None):
                with mock.patch(
                    "center.deploy_errors.subprocess.run", side_effect=fake_run
                ):
                    proc = run_ssh_with_retry(
                        ["ssh", "x"], stdin_path=path, attempts=3
                    )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(calls["data"][0], payload)
            self.assertEqual(calls["data"][1], payload)
            self.assertEqual(proc.stdout, "ok\n")
        finally:
            os.remove(path)


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
        self.assertIn("package_incomplete", src)
        self.assertNotIn("README.md 2>/dev/null", src)
        self.assertNotIn("agent center common", src)
        self.assertNotIn('"$ROOT/" "${SSH_USER}', src)
        self.assertIn('"$ROOT/$part/"', src)
        self.assertIn("package_incomplete", asrc)
        self.assertIn("sync_agent_tree", asrc)
        self.assertIn("assert_package_tree", asrc)
        self.assertIn("stop_previous_agent", asrc)
        self.assertLess(asrc.find("stop_previous_agent"), asrc.find("sync_agent_tree"))
        once_at = asrc.find("--once")
        self.assertGreater(once_at, 0)
        self.assertIn("agent/__init__.py", asrc[:once_at])
        self.assertIn("/tmp/monitor-agent-src", src)
        self.assertNotIn("tar -xzf /tmp/monitor-agent.tgz -C '$REMOTE_DIR'", src)
        self.assertNotIn("| grep -q 'agent/__init__.py'", src)
        self.assertIn("tar -tzf /tmp/monitor-agent.tgz agent/__init__.py", src)
        self.assertIn('cat > /tmp/monitor-agent.tgz', src)
        self.assertNotIn("本机无 scp", src)
        self.assertNotIn('ssh_run_retry "$ip" scp ', src)
        center = os.path.join(ROOT, "scripts", "deploy_center.sh")
        with open(center, encoding="utf-8") as f:
            csrc = f.read()
        self.assertIn("assert_package_tree", csrc)
        self.assertIn('assert_package_tree "$ROOT"', csrc)
        self.assertIn('assert_package_tree "$INSTALL_DIR"', csrc)

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
            code="$(ssh_classify_fail $'bash: scp: command not found\\nlost connection\\n')"
            echo "SCP_MISS=$code"
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
        self.assertIn("SCP_MISS=remote_fail", proc.stdout)


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

    def test_run_root_prints_sudo_required_when_sudo_fails(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root 下 run_root 不会走 sudo")
        helper = build_remote_root_helper(
            {"mode": "password", "password": "bad-pass"}, use_sudo=True
        )
        script = (
            "set -euo pipefail\n"
            + helper
            + textwrap.dedent(
                """
                sudo() { echo "sudo: a password is required" >&2; return 1; }
                run_root true
                echo SHOULD_NOT_REACH
                """
            )
        )
        proc = subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("sudo_required", proc.stdout)
        self.assertNotIn("SHOULD_NOT_REACH", proc.stdout)

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

    def test_upload_cmd_uses_ssh_cat_not_scp(self) -> None:
        auth = {"mode": "password", "password": "secret", "key_path": ""}
        with mock.patch(
            "center.deploy_runner.shutil.which", return_value="/usr/bin/sshpass"
        ):
            ssh_cmd, scp_cmd, _env, _cleanup = build_ssh_scp_cmds(
                ssh_port=22, auth=auth
            )
        upload = build_ssh_upload_cmd(ssh_cmd, "root@10.0.0.9")
        self.assertEqual(upload[0], "sshpass")
        self.assertIn("ssh", upload)
        self.assertNotIn("scp", upload)
        self.assertIn("-T", upload)
        self.assertEqual(upload[-2], "root@10.0.0.9")
        self.assertTrue(upload[-1].startswith("cat >"))
        self.assertIn("/tmp/monitor-agent.tgz", upload[-1])
        # scp_cmd 仍会构造，但产品上传路径不再调用它
        self.assertIn("scp", scp_cmd)


class SshPipeUploadTests(unittest.TestCase):
    def test_upload_file_via_ssh_writes_binary(self) -> None:
        tmp = tempfile.mkdtemp(prefix="ssh-pipe-")
        try:
            src = os.path.join(tmp, "src.bin")
            dst = os.path.join(tmp, "dst.bin")
            payload = b"\x1f\x8b gzip-magic \x00\xff\r\n\n" + os.urandom(2048)
            with open(src, "wb") as f:
                f.write(payload)
            fake_ssh = os.path.join(tmp, "ssh")
            with open(fake_ssh, "w", encoding="utf-8") as f:
                f.write(
                    "#!/usr/bin/env bash\n"
                    "set -euo pipefail\n"
                    'cmd="${!#}"\n'
                    'eval "$cmd"\n'
                )
            os.chmod(fake_ssh, 0o755)
            proc = upload_file_via_ssh(
                [fake_ssh],
                "root@host",
                src,
                remote_path=dst,
                env=dict(os.environ),
                timeout=10,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            with open(dst, "rb") as f:
                self.assertEqual(f.read(), payload)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fleet_ssh_run_retry_pipes_stdin(self) -> None:
        script = textwrap.dedent(
            """
            set -euo pipefail
            . "{root}/scripts/lib/deploy_ssh.sh"
            src="$(mktemp)"
            dst="$(mktemp)"
            trap 'rm -f "$src" "$dst"' EXIT
            python3 - <<'PY' > "$src"
            import sys
            sys.stdout.buffer.write(b"gz\\x1f\\x8b\\x00\\xffdata")
            PY
            out="$(ssh_run_retry lab bash -c "cat > '$dst'" < "$src")"
            python3 -c "import sys; a=open(sys.argv[1],'rb').read(); b=open(sys.argv[2],'rb').read(); sys.exit(0 if a==b else 1)" "$src" "$dst"
            echo PIPE_OK
            """
        ).format(root=ROOT)
        proc = subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("PIPE_OK", proc.stdout)


class PackageTreeTests(unittest.TestCase):
    def test_repo_root_complete(self) -> None:
        self.assertEqual(missing_package_parts(ROOT), [])
        self.assertEqual(REQUIRED_PACKAGE_PARTS, ("agent", "common", "scripts"))
        self.assertNotIn("center", OPTIONAL_PACKAGE_PARTS)

    def test_make_package_fails_hard_without_agent(self) -> None:
        tmp = tempfile.mkdtemp(prefix="pkg-miss-")
        try:
            os.makedirs(os.path.join(tmp, "common"))
            os.makedirs(os.path.join(tmp, "scripts"))
            with open(os.path.join(tmp, "README.md"), "w", encoding="utf-8") as f:
                f.write("x")
            with self.assertRaises(DeployError) as ctx:
                _make_package_tgz(tmp)
            self.assertEqual(ctx.exception.code, PACKAGE_INCOMPLETE)
            self.assertIn("agent", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_make_package_fails_hard_without_common(self) -> None:
        tmp = tempfile.mkdtemp(prefix="pkg-miss-")
        try:
            os.makedirs(os.path.join(tmp, "agent"))
            os.makedirs(os.path.join(tmp, "scripts"))
            with open(os.path.join(tmp, "agent", "__init__.py"), "w", encoding="utf-8") as f:
                f.write("")
            with self.assertRaises(DeployError) as ctx:
                _make_package_tgz(tmp)
            self.assertEqual(ctx.exception.code, PACKAGE_INCOMPLETE)
            self.assertIn("common", str(ctx.exception))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_make_package_tgz_contains_agent_init(self) -> None:
        import tarfile

        path = _make_package_tgz(ROOT)
        try:
            with tarfile.open(path, "r:gz") as tar:
                names = set(tar.getnames())
            self.assertIn("agent/__init__.py", names)
            self.assertTrue(any(n == "common" or n.startswith("common/") for n in names))
            self.assertTrue(any(n == "scripts" or n.startswith("scripts/") for n in names))
            self.assertFalse(any(n == "center" or n.startswith("center/") for n in names))
        finally:
            os.remove(path)

    def test_assert_package_tree_bash(self) -> None:
        script = textwrap.dedent(
            """
            set -euo pipefail
            . "{root}/scripts/lib/sync_tree.sh"
            assert_package_tree "{root}" "repo"
            echo SOURCE_OK
            tmp="$(mktemp -d)"
            trap 'rm -rf "$tmp"' EXIT
            if assert_package_tree "$tmp" "empty"; then
              echo SHOULD_NOT
              exit 1
            fi
            echo EMPTY_CAUGHT
            """
        ).format(root=ROOT)
        proc = subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SOURCE_OK", proc.stdout)
        self.assertIn("EMPTY_CAUGHT", proc.stdout)
        self.assertIn("package_incomplete", proc.stdout)

    def test_sync_agent_tree_skips_center(self) -> None:
        src = tempfile.mkdtemp(prefix="agent-src-")
        dst = tempfile.mkdtemp(prefix="agent-dst-")
        try:
            for name in ("agent", "common", "scripts", "center"):
                os.makedirs(os.path.join(src, name))
            with open(os.path.join(src, "agent", "__init__.py"), "w", encoding="utf-8") as f:
                f.write("")
            with open(os.path.join(src, "center", "server.py"), "w", encoding="utf-8") as f:
                f.write("nope")
            script = textwrap.dedent(
                """
                set -euo pipefail
                . "{root}/scripts/lib/sync_tree.sh"
                sync_agent_tree "{src}" "{dst}"
                """
            ).format(root=ROOT, src=src, dst=dst)
            proc = subprocess.run(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dst, "agent", "__init__.py")))
            self.assertTrue(os.path.isdir(os.path.join(dst, "common")))
            self.assertTrue(os.path.isdir(os.path.join(dst, "scripts")))
            self.assertFalse(os.path.isdir(os.path.join(dst, "center")))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_sync_cleans_leftovers_keeps_agent_json_and_data(self) -> None:
        src = tempfile.mkdtemp(prefix="agent-src-")
        dst = tempfile.mkdtemp(prefix="agent-dst-")
        try:
            for name in ("agent", "common", "scripts"):
                os.makedirs(os.path.join(src, name))
            with open(os.path.join(src, "agent", "__init__.py"), "w", encoding="utf-8") as f:
                f.write("new")
            os.makedirs(os.path.join(dst, "agent", "__pycache__"))
            os.makedirs(os.path.join(dst, "center"))
            os.makedirs(os.path.join(dst, "config"))
            os.makedirs(os.path.join(dst, "data"))
            with open(os.path.join(dst, "agent", "__init__.py"), "w", encoding="utf-8") as f:
                f.write("old")
            with open(os.path.join(dst, "agent", "gone.py"), "w", encoding="utf-8") as f:
                f.write("stale")
            with open(os.path.join(dst, "agent", "._secret"), "w", encoding="utf-8") as f:
                f.write("appledouble")
            with open(os.path.join(dst, "agent", "__pycache__", "x.pyc"), "w", encoding="utf-8") as f:
                f.write("pyc")
            with open(os.path.join(dst, "center", "server.py"), "w", encoding="utf-8") as f:
                f.write("accidental")
            with open(os.path.join(dst, "config", "agent.json"), "w", encoding="utf-8") as f:
                f.write('{"keep":true}')
            with open(os.path.join(dst, "data", "metrics.db"), "w", encoding="utf-8") as f:
                f.write("db")
            script = textwrap.dedent(
                """
                set -euo pipefail
                . "{root}/scripts/lib/sync_tree.sh"
                sync_agent_tree "{src}" "{dst}"
                """
            ).format(root=ROOT, src=src, dst=dst)
            proc = subprocess.run(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dst, "agent", "__init__.py")))
            self.assertFalse(os.path.isfile(os.path.join(dst, "agent", "gone.py")))
            self.assertFalse(os.path.isfile(os.path.join(dst, "agent", "._secret")))
            self.assertFalse(os.path.isdir(os.path.join(dst, "agent", "__pycache__")))
            self.assertFalse(os.path.isdir(os.path.join(dst, "center")))
            with open(os.path.join(dst, "config", "agent.json"), encoding="utf-8") as f:
                self.assertIn("keep", f.read())
            self.assertTrue(os.path.isfile(os.path.join(dst, "data", "metrics.db")))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_prune_keeps_center_on_monitor_basename(self) -> None:
        parent = tempfile.mkdtemp(prefix="monitor-parent-")
        dst = os.path.join(parent, "monitor")
        try:
            os.makedirs(os.path.join(dst, "center"))
            os.makedirs(os.path.join(dst, "agent"))
            with open(os.path.join(dst, "center", "server.py"), "w", encoding="utf-8") as f:
                f.write("keep")
            with open(os.path.join(dst, "agent", "._x"), "w", encoding="utf-8") as f:
                f.write("junk")
            script = textwrap.dedent(
                """
                set -euo pipefail
                . "{root}/scripts/lib/sync_tree.sh"
                prune_agent_install_dir "{dst}"
                """
            ).format(root=ROOT, dst=dst)
            proc = subprocess.run(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertTrue(os.path.isfile(os.path.join(dst, "center", "server.py")))
            self.assertFalse(os.path.isfile(os.path.join(dst, "agent", "._x")))
        finally:
            shutil.rmtree(parent, ignore_errors=True)

    def test_stop_previous_agent_kills_pidfile(self) -> None:
        dst = tempfile.mkdtemp(prefix="agent-run-")
        proc = None
        try:
            os.makedirs(os.path.join(dst, "run"))
            proc = subprocess.Popen(["sleep", "30"])
            with open(os.path.join(dst, "run", "agent.pid"), "w", encoding="utf-8") as f:
                f.write(str(proc.pid))
            script = textwrap.dedent(
                """
                set -euo pipefail
                export MONITOR_CLEANUP_SKIP_PKILL=1
                . "{root}/scripts/lib/sync_tree.sh"
                stop_previous_agent "{dst}"
                """
            ).format(root=ROOT, dst=dst)
            out = subprocess.run(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertEqual(out.returncode, 0, out.stdout)
            proc.wait(timeout=5)
            self.assertNotEqual(proc.returncode, None)
            self.assertFalse(os.path.isfile(os.path.join(dst, "run", "agent.pid")))
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
            shutil.rmtree(dst, ignore_errors=True)

    def test_sync_agent_tree_fails_without_agent(self) -> None:
        src = tempfile.mkdtemp(prefix="agent-miss-")
        dst = tempfile.mkdtemp(prefix="agent-dst-")
        try:
            os.makedirs(os.path.join(src, "common"))
            os.makedirs(os.path.join(src, "scripts"))
            os.makedirs(os.path.join(src, "center"))
            script = textwrap.dedent(
                """
                set -euo pipefail
                . "{root}/scripts/lib/sync_tree.sh"
                sync_agent_tree "{src}" "{dst}"
                """
            ).format(root=ROOT, src=src, dst=dst)
            proc = subprocess.run(
                ["bash", "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("package_incomplete", proc.stdout)
            self.assertFalse(os.path.isfile(os.path.join(dst, "agent", "__init__.py")))
        finally:
            shutil.rmtree(src, ignore_errors=True)
            shutil.rmtree(dst, ignore_errors=True)

    def test_runner_extract_asserts_before_deploy_agent(self) -> None:
        path = os.path.join(ROOT, "center", "deploy_runner.py")
        with open(path, encoding="utf-8") as f:
            src = f.read()
        extract_at = src.find("tar -xzf /tmp/monitor-agent.tgz")
        self.assertGreater(extract_at, 0)
        self.assertIn("/tmp/monitor-agent-src", src)
        self.assertNotIn(
            'tar -xzf /tmp/monitor-agent.tgz -C "$REMOTE_DIR"',
            src,
        )
        deploy_at = src.find("deploy_agent.sh", extract_at)
        self.assertGreater(deploy_at, extract_at)
        between = src[extract_at:deploy_at]
        self.assertIn("agent/__init__.py", between)
        self.assertIn("package_incomplete", between)
        self.assertIn("tar -tzf /tmp/monitor-agent.tgz agent/__init__.py", src)
        self.assertNotIn("| grep -q 'agent/__init__.py'", src)
        self.assertNotIn(
            "if os.path.exists(full):\n                tar.add(full, arcname=name)",
            src,
        )
        self.assertNotIn('OPTIONAL_PACKAGE_PARTS = ("center"', src)
        self.assertIn("upload_file_via_ssh", src)
        self.assertIn("cat >", src)
        self.assertIn("不依赖远端 scp", src)
        self.assertNotIn("scp_base + [tgz", src)

    def _run_bash(self, script: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            ["bash", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def test_pipefail_tar_grep_q_false_positive_on_complete_package(self) -> None:
        """pipefail + tar|grep -q：grep 命中后 tar 得 SIGPIPE(141)，好包装被判 package_incomplete。"""
        fd, path = tempfile.mkstemp(prefix="pkg-sigpipe-", suffix=".tgz")
        os.close(fd)
        try:
            with tarfile.open(path, "w:gz") as tar:
                payload = b"ok\n"
                info = tarfile.TarInfo(name="agent/__init__.py")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
                blob = b"x" * 256
                for i in range(400):
                    extra = tarfile.TarInfo(name="padding/%04d.bin" % i)
                    extra.size = len(blob)
                    tar.addfile(extra, io.BytesIO(blob))
            script = textwrap.dedent(
                """
                set -euo pipefail
                TGZ={tgz}
                tar -tzf "$TGZ" >/dev/null
                echo TAR_LIST_OK
                if ! tar -tzf "$TGZ" | grep -q 'agent/__init__.py'; then
                  echo "[fail] package_incomplete: 安装包不含 agent/"
                  exit 1
                fi
                echo CHECK_OK
                """
            ).format(tgz=shlex.quote(path))
            proc = self._run_bash(script)
            self.assertIn("TAR_LIST_OK", proc.stdout)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("package_incomplete", proc.stdout)
            self.assertNotIn("CHECK_OK", proc.stdout)
        finally:
            os.remove(path)

    def test_explicit_member_check_accepts_complete_package_under_pipefail(self) -> None:
        tgz = _make_package_tgz(ROOT)
        try:
            script = textwrap.dedent(
                """
                set -euo pipefail
                TGZ={tgz}
                if ! tar -tzf "$TGZ" agent/__init__.py >/dev/null 2>&1; then
                  echo "[fail] package_incomplete: 安装包不含 agent/"
                  exit 1
                fi
                echo CHECK_OK
                """
            ).format(tgz=shlex.quote(tgz))
            proc = self._run_bash(script)
            self.assertEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("CHECK_OK", proc.stdout)
            self.assertNotIn("package_incomplete", proc.stdout)
        finally:
            os.remove(tgz)

    def test_explicit_member_check_hard_fails_without_agent(self) -> None:
        fd, path = tempfile.mkstemp(prefix="pkg-empty-", suffix=".tgz")
        os.close(fd)
        try:
            with tarfile.open(path, "w:gz") as tar:
                payload = b"not-agent"
                info = tarfile.TarInfo(name="common/placeholder.txt")
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            script = textwrap.dedent(
                """
                set -euo pipefail
                TGZ={tgz}
                if ! tar -tzf "$TGZ" agent/__init__.py >/dev/null 2>&1; then
                  echo "[fail] package_incomplete: 安装包不含 agent/"
                  exit 1
                fi
                echo CHECK_OK
                """
            ).format(tgz=shlex.quote(path))
            proc = self._run_bash(script)
            self.assertNotEqual(proc.returncode, 0, proc.stdout)
            self.assertIn("package_incomplete", proc.stdout)
            self.assertNotIn("CHECK_OK", proc.stdout)
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
