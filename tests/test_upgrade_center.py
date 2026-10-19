"""中心升级：配置与 Environment 保留，测试失败时回滚。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center import api as api_mod
from center.storage import Storage


def _tree(path: str, files: dict) -> None:
    for rel, content in files.items():
        full = os.path.join(path, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(content)


def _ok_test() -> str:
    return textwrap.dedent(
        """
        import unittest

        class Ok(unittest.TestCase):
            def test_ok(self):
                self.assertTrue(True)
        """
    )


def _boom_test() -> str:
    return textwrap.dedent(
        """
        import unittest

        class Boom(unittest.TestCase):
            def test_boom(self):
                self.fail("boom")
        """
    )


class UpgradeCenterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="upgrade-")
        self.install = os.path.join(self.tmp, "monitor")
        self.pkg = os.path.join(self.tmp, "pkg")
        self.unit = os.path.join(self.tmp, "monitor-center.service")
        self.curl = os.path.join(self.tmp, "curl")
        self.systemctl = os.path.join(self.tmp, "systemctl")
        self.syslog = os.path.join(self.tmp, "systemctl.log")
        os.makedirs(self.install)
        with open(self.curl, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/bash\n"
                "url=\"\"\n"
                "for a in \"$@\"; do\n"
                "  case \"$a\" in\n"
                "    http*) url=\"$a\" ;;\n"
                "  esac\n"
                "done\n"
                "case \"$url\" in\n"
                "  */api/v1/health) printf '%s\\n' '{\"ok\":true,\"version\":\"1.9.0\"}' ;;\n"
                "  */api/v1/hosts) printf '%s\\n' '{\"hosts\":[]}' ;;\n"
                "  */api/v1/reports) printf '%s\\n' '{\"ok\":true}' ;;\n"
                "  *) echo \"unexpected $url\" >&2; exit 1 ;;\n"
                "esac\n"
            )
        with open(self.systemctl, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/bash\n"
                "printf '%%s\\n' \"$*\" >> \"%s\"\n"
                "exit 0\n" % self.syslog
            )
        os.chmod(self.curl, 0o755)
        os.chmod(self.systemctl, 0o755)
        self.original_unit = textwrap.dedent(
            """
            [Unit]
            Description=简易多机监控中心端

            [Service]
            WorkingDirectory=/opt/monitor
            Environment=PYTHONPATH=/opt/monitor
            Environment=LD_LIBRARY_PATH=/usr/local/python3.8/lib
            Environment=PATH=/usr/local/bin:/usr/local/python3.8/bin:/usr/bin:/bin
            Environment=FOO=bar
            ExecStart=/usr/local/python3.8/bin/python3 -m center --config /opt/monitor/config/center.json
            """
        ).lstrip()
        with open(self.unit, "w", encoding="utf-8") as fh:
            fh.write(self.original_unit)
        _tree(
            self.install,
            {
                "agent/__init__.py": "# agent\n",
                "common/keep.txt": "common\n",
                "scripts/keep.sh": "#!/bin/bash\n",
                "config/center.json": json.dumps(
                    {"port": 9, "marker": "KEEP-ME", "token": "secret"}
                ),
                "build-id.txt": "old-marker\n",
                "tests/test_ok.py": _ok_test(),
                "vendor/python/already.tar.gz": "bundle-bytes\n",
            },
        )
        db = os.path.join(self.install, "data", "monitor.db")
        os.makedirs(os.path.dirname(db), exist_ok=True)
        storage = Storage(db, offline_seconds=90, retention_days=7)
        now = int(time.time())
        api_mod.handle_metrics_post(
            storage,
            json.dumps(
                {
                    "host_id": "host-a",
                    "hostname": "host-a",
                    "host_type": "cpu",
                    "timestamp": now,
                    "system": {"cpu_percent": 1, "mem_percent": 1, "disk_percent": 1},
                }
            ).encode("utf-8"),
        )
        self.base_env = dict(os.environ)
        self.base_env.pop("MONITOR_SYNC_DISABLE_RSYNC", None)
        self.base_env.pop("MONITOR_SYNC_DISABLE_TAR", None)
        self.base_env.update(
            {
                "MONITOR_UPGRADE_UNIT": self.unit,
                "MONITOR_UPGRADE_CURL_CMD": self.curl,
                "MONITOR_UPGRADE_SYSTEMCTL_CMD": self.systemctl,
                "MONITOR_UPGRADE_PYTHON": sys.executable,
                "MONITOR_UPGRADE_HEALTH_TRIES": "1",
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _package(self, files: dict) -> None:
        if os.path.isdir(self.pkg):
            shutil.rmtree(self.pkg)
        _tree(
            self.pkg,
            {
                "agent/__init__.py": "# agent new\n",
                "common/keep.txt": "common\n",
                "scripts/keep.sh": "#!/bin/bash\n",
                "config/center.json": json.dumps({"port": 1, "marker": "PACKAGE"}),
                **files,
            },
        )

    def _run(self, extra):
        cmd = [
            "bash",
            os.path.join(ROOT, "scripts", "upgrade_center.sh"),
            self.pkg,
            "--dir",
            self.install,
            "--sha",
            "abc123",
            "--keep-backups",
            "2",
        ]
        return subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=self.base_env,
            timeout=60,
        )

    def test_preserves_config_environment_and_vendor_bundle(self) -> None:
        for name in ("100", "200", "300"):
            os.makedirs(os.path.join(self.tmp, "monitor-backup-" + name))
        self._package({"build-id.txt": "brand-new-marker\n", "tests/test_ok.py": _ok_test()})
        proc = self._run(None)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("升级完成", proc.stdout)
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["marker"], "KEEP-ME")
        self.assertEqual(cfg["token"], "secret")
        with open(os.path.join(self.install, "build-id.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "brand-new-marker")
        with open(os.path.join(self.install, "vendor", "python", "already.tar.gz"), encoding="utf-8") as fh:
            self.assertIn("bundle-bytes", fh.read())
        with open(self.unit, encoding="utf-8") as fh:
            unit = fh.read()
        self.assertIn("Environment=PATH=/usr/local/bin:/usr/local/python3.8/bin:/usr/bin:/bin", unit)
        self.assertIn("Environment=FOO=bar", unit)
        self.assertIn("Environment=LD_LIBRARY_PATH=/usr/local/python3.8/lib", unit)
        self.assertIn("Environment=PYTHONPATH=%s" % self.install, unit)
        self.assertIn(
            "ExecStart=/usr/local/python3.8/bin/python3 -m center --config /opt/monitor/config/center.json",
            unit,
        )
        with open(os.path.join(self.install, ".merge_sha"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "abc123")
        with open(self.syslog, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("restart monitor-center", log)
        self.assertIn("is-active --quiet monitor-center", log)
        names = [
            name
            for name in os.listdir(self.tmp)
            if name.startswith("monitor-backup-")
        ]
        self.assertEqual(len(names), 2)
        self.assertNotIn("monitor-backup-100", names)
        self.assertNotIn("monitor-backup-200", names)
        conn_hosts = subprocess.check_output(
            [
                sys.executable,
                "-c",
                "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); print(c.execute('select count(*) from hosts').fetchone()[0], c.execute('select count(*) from metrics_history').fetchone()[0])",
                os.path.join(self.install, "data", "monitor.db"),
            ],
            universal_newlines=True,
        )
        hosts, hist = conn_hosts.split()
        self.assertEqual(hosts, "1")
        self.assertGreaterEqual(int(hist), 1)

    def test_failed_package_tests_abort_before_sync(self) -> None:
        live = os.path.join(self.install, "data", "live-only.txt")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("live\n")
        db = os.path.join(self.install, "data", "monitor.db")
        inode = os.stat(db).st_ino
        self._package(
            {"build-id.txt": "brand-new-marker\n", "tests/test_boom.py": _boom_test()}
        )
        proc = self._run(None)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("未改动安装目录", proc.stdout)
        self.assertNotIn("同步代码", proc.stdout)
        self.assertNotIn("[rollback]", proc.stdout)
        with open(os.path.join(self.install, "build-id.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "old-marker")
        with open(self.unit, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.original_unit)
        self.assertEqual(os.stat(db).st_ino, inode)
        with open(live, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "live\n")
        self.assertFalse(os.path.isfile(os.path.join(self.install, "tests", "test_boom.py")))
        names = os.listdir(self.tmp)
        self.assertFalse(any(name.startswith("monitor-backup-") for name in names))
        self.assertFalse(any(name.startswith("monitor-failed-") for name in names))
        if os.path.isfile(self.syslog):
            with open(self.syslog, encoding="utf-8") as fh:
                self.assertNotIn("restart", fh.read())

    def test_health_rollback_keeps_data_and_failed_tree(self) -> None:
        live = os.path.join(self.install, "data", "live-only.txt")
        during = os.path.join(self.install, "data", "written-during-upgrade")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("live\n")
        db = os.path.join(self.install, "data", "monitor.db")
        inode = os.stat(db).st_ino
        with open(self.systemctl, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/bash\n"
                "printf '%%s\\n' \"$*\" >> \"%s\"\n"
                "case \"$*\" in\n"
                "  *restart*) printf 'during\\n' >> \"%s\" ;;\n"
                "esac\n"
                "exit 0\n" % (self.syslog, during)
            )
        os.chmod(self.systemctl, 0o755)
        with open(self.curl, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/bash\necho down >&2\nexit 1\n")
        os.chmod(self.curl, 0o755)
        self._package({"build-id.txt": "brand-new-marker\n", "tests/test_ok.py": _ok_test()})
        proc = self._run(None)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("回滚代码", proc.stdout)
        self.assertIn("不改 data/", proc.stdout)
        with open(os.path.join(self.install, "build-id.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "old-marker")
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["marker"], "KEEP-ME")
        with open(self.unit, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.original_unit)
        self.assertEqual(os.stat(db).st_ino, inode)
        with open(live, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "live\n")
        with open(during, encoding="utf-8") as fh:
            self.assertIn("during", fh.read())
        failed = [name for name in os.listdir(self.tmp) if name.startswith("monitor-failed-")]
        self.assertEqual(len(failed), 1, proc.stdout)
        failed_dir = os.path.join(self.tmp, failed[0])
        with open(os.path.join(failed_dir, "build-id.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "brand-new-marker")
        self.assertFalse(os.path.isdir(os.path.join(failed_dir, "data")))
        self.assertTrue(os.path.isdir(os.path.join(self.install, "data")))
        with open(os.path.join(ROOT, "scripts", "upgrade_center.sh"), encoding="utf-8") as fh:
            src = fh.read()
        self.assertNotIn('rm -rf "$INSTALL_DIR"', src)
        self.assertNotIn('rm -rf "$failed_dir"', src)
        self.assertNotIn('rm -rf "$INSTALL_DIR/data"', src)


def _write_exe(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    os.chmod(path, 0o755)


class PushSshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="push-ssh-")
        self.bin = os.path.join(self.tmp, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(self.tmp, "ssh.log")
        _write_exe(
            os.path.join(self.bin, "sshpass"),
            "#!/bin/bash\nprintf 'sshpass %%s\\n' \"$*\" >> \"%s\"\nexit 0\n" % self.log,
        )
        _write_exe(
            os.path.join(self.bin, "fake-ssh"),
            "#!/bin/bash\nprintf '%%s\\n' \"$*\" >> \"%s\"\ncat >/dev/null\nexit 0\n" % self.log,
        )
        self.env = dict(os.environ)
        self.env.pop("SSHPASS", None)
        self.env.pop("MONITOR_SSH_CMD_OVERRIDE", None)
        self.env["PATH"] = self.bin + os.pathsep + "/usr/bin:/bin"
        self.env["MONITOR_PUSH_DRY"] = "1"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sshpass_env_wraps_ssh(self) -> None:
        self.env["SSHPASS"] = "secret-token"
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "push_upgrade.sh"), "root@center", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=self.env,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=sshpass -e ssh", proc.stdout)
        self.assertNotIn("secret-token", proc.stdout)
        self.assertFalse(os.path.exists(self.log))

    def test_plain_ssh_without_sshpass_env(self) -> None:
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "push_upgrade.sh"), "root@center", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=self.env,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=ssh\n", proc.stdout)
        self.assertNotIn("sshpass", proc.stdout)

    def test_ssh_cmd_override_skips_sshpass(self) -> None:
        self.env["SSHPASS"] = "secret-token"
        proc = subprocess.run(
            [
                "bash",
                os.path.join(ROOT, "scripts", "push_upgrade.sh"),
                "--ssh-cmd",
                "fake-ssh -o BatchMode=yes",
                "root@center",
                "HEAD",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=self.env,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=fake-ssh -o BatchMode=yes", proc.stdout)
        self.assertNotIn("sshpass", proc.stdout)

    def test_sshpass_set_without_binary_fails(self) -> None:
        clean = os.path.join(self.tmp, "clean-bin")
        os.makedirs(clean)
        for name in ("bash", "dirname", "git", "ssh"):
            src = shutil.which(name)
            self.assertTrue(src, name)
            os.symlink(src, os.path.join(clean, name))
        self.env["SSHPASS"] = "secret-token"
        self.env["PATH"] = clean
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "push_upgrade.sh"), "root@center", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=self.env,
            timeout=20,
        )
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("没有 sshpass", proc.stdout)

    def test_push_upgrade_plans_bundle_mkdir(self) -> None:
        bundle = os.path.join(self.tmp, "vendor")
        os.makedirs(bundle)
        with open(os.path.join(bundle, "cpython.tar.gz"), "wb") as fh:
            fh.write(b"bundle")
        self.env["MONITOR_PUSH_BUNDLE_DIR"] = bundle
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "push_upgrade.sh"), "root@center", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=self.env,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("BUNDLES=sync", proc.stdout)
        self.assertIn("MKDIR=/opt/monitor/vendor/python", proc.stdout)

    def test_push_bundles_mkdirs_missing_remote_dir(self) -> None:
        local = os.path.join(self.tmp, "local-bundles")
        os.makedirs(local)
        with open(os.path.join(local, "bundles.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with open(os.path.join(local, "cpython.tar.gz"), "wb") as fh:
            fh.write(b"bundle")
        env = dict(self.env)
        env.pop("MONITOR_PUSH_DRY", None)
        proc = subprocess.run(
            [
                "bash",
                os.path.join(ROOT, "scripts", "push_python_bundles.sh"),
                "--ssh-cmd",
                os.path.join(self.bin, "fake-ssh"),
                "root@center",
                local,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=env,
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        with open(self.log, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("mkdir -p '/opt/monitor/vendor/python'", log)
        self.assertIn("tar -xf - -C '/opt/monitor/vendor/python'", log)
