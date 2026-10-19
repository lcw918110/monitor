"""中心升级：配置与 Environment 保留，测试失败时回滚。"""

from __future__ import annotations

import json
import os
import shlex
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


def _captured(cmd, env, timeout):
    """按 UTF-8 解码。macOS 上 LANG=C 时 universal_newlines 会把中文输出解成 UnicodeDecodeError。"""
    return subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )


@unittest.skipIf(
    sys.platform == "darwin",
    "upgrade_center.sh 使用 GNU stat -c%s 和 GNU df；macOS 的 BSD stat/df 对不上，在 Darwin 上跳过",
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

    def _run(self, extra, env=None, script=None):
        cmd = [
            "bash",
            script or os.path.join(ROOT, "scripts", "upgrade_center.sh"),
            self.pkg,
            "--dir",
            self.install,
            "--sha",
            "abc123",
            "--keep-backups",
            "2",
        ]
        if extra:
            cmd.extend(extra)
        return _captured(cmd, env or self.base_env, 90)

    def _ld_bound_python(self, libdir):
        """解释器在 LD_LIBRARY_PATH 含 libdir 之前直接失败，模拟 libpython3.8.so.1.0。"""
        os.makedirs(libdir, exist_ok=True)
        open(os.path.join(libdir, "libpython3.8.so.1.0"), "a").close()
        wrapper = os.path.join(self.tmp, "python-needs-ld")
        marker = os.path.join(self.tmp, "python-used.txt")
        body = "\n".join(
            [
                "#!/bin/bash",
                'case ":${LD_LIBRARY_PATH:-}:" in',
                "  *:%s:*) ;;" % libdir,
                "  *)",
                '    echo "error while loading shared libraries: libpython3.8.so.1.0" >&2',
                "    exit 127",
                "    ;;",
                "esac",
                "printf 'used\\n' >> %s" % shlex.quote(marker),
                "exec %s \"$@\"" % shlex.quote(sys.executable),
                "",
            ]
        )
        _write_exe(wrapper, body)
        return wrapper, marker

    def _env_without_python_override(self):
        env = dict(self.base_env)
        env.pop("MONITOR_UPGRADE_PYTHON", None)
        env.pop("LD_LIBRARY_PATH", None)
        env.pop("PYTHONPATH", None)
        return env

    def test_execstart_python_requires_unit_ld_library_path(self) -> None:
        libdir = os.path.join(self.tmp, "py38", "lib")
        wrapper, marker = self._ld_bound_python(libdir)
        with open(self.unit, "w", encoding="utf-8") as fh:
            fh.write(
                "\n".join(
                    [
                        "[Service]",
                        "Environment=LD_LIBRARY_PATH=%s" % libdir,
                        "Environment=PATH=/usr/bin:/bin",
                        "ExecStart=%s -m center --config %s"
                        % (wrapper, os.path.join(self.install, "config", "center.json")),
                        "",
                    ]
                )
            )
        self._package({"build-id.txt": "brand-new-marker\n", "tests/test_ok.py": _ok_test()})
        proc = self._run(None, env=self._env_without_python_override())
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("升级完成", proc.stdout)
        self.assertNotIn("libpython3.8.so.1.0", proc.stdout)
        with open(marker, encoding="utf-8") as fh:
            self.assertIn("used", fh.read())
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["marker"], "KEEP-ME")

    def _upgrade_script_from_archive(self) -> str:
        if os.path.isdir(os.path.join(ROOT, ".git")) and shutil.which("git"):
            extract = tempfile.mkdtemp(prefix="upgrade-src-")
            self.addCleanup(shutil.rmtree, extract, True)
            archive = subprocess.check_output(
                ["git", "-C", ROOT, "archive", "HEAD"],
                stdin=subprocess.DEVNULL,
            )
            subprocess.run(["tar", "-x", "-C", extract], input=archive, check=True)
            script = os.path.join(extract, "scripts", "upgrade_center.sh")
            self.assertTrue(os.path.isfile(script), script)
            with open(script, encoding="utf-8") as fh:
                text = fh.read()
            self.assertIn("_upgrade_apply_unit_runtime_env", text)
            return script
        return os.path.join(ROOT, "scripts", "upgrade_center.sh")

    def test_archive_script_preserves_config_and_data_with_unit_env(self) -> None:
        libdir = os.path.join(self.tmp, "usr", "local", "python3.8", "lib")
        wrapper, marker = self._ld_bound_python(libdir)
        live = os.path.join(self.install, "data", "live-only.txt")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("live\n")
        db = os.path.join(self.install, "data", "monitor.db")
        inode = os.stat(db).st_ino
        recorded = os.path.join(self.tmp, "seen-pythonpath.txt")
        with open(self.unit, "w", encoding="utf-8") as fh:
            fh.write(
                "\n".join(
                    [
                        "[Service]",
                        "Environment=PATH=/usr/local/bin:/usr/local/python3.8/bin:/usr/bin:/bin",
                        "Environment=LD_LIBRARY_PATH=/usr/local/python3.8/lib:%s" % libdir,
                        "Environment=PYTHONPATH=/opt/monitor",
                        "ExecStart=%s -m center --config %s"
                        % (wrapper, os.path.join(self.install, "config", "center.json")),
                        "",
                    ]
                )
            )
        py_test = textwrap.dedent(
            """
            import os
            import unittest

            class Ok(unittest.TestCase):
                def test_records_pythonpath(self):
                    with open(%s, "w", encoding="utf-8") as fh:
                        fh.write(os.environ.get("PYTHONPATH", ""))
            """
            % json.dumps(recorded)
        )
        self._package({"build-id.txt": "brand-new-marker\n", "tests/test_ok.py": py_test})
        proc = self._run(
            None,
            env=self._env_without_python_override(),
            script=self._upgrade_script_from_archive(),
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("升级完成", proc.stdout)
        with open(marker, encoding="utf-8") as fh:
            self.assertIn("used", fh.read())
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertEqual(cfg["marker"], "KEEP-ME")
        self.assertEqual(cfg["token"], "secret")
        self.assertEqual(os.stat(db).st_ino, inode)
        with open(live, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "live\n")
        with open(recorded, encoding="utf-8") as fh:
            seen = fh.read()
        self.assertEqual(os.path.realpath(seen), os.path.realpath(self.pkg))
        self.assertNotIn("/opt/monitor", seen)

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
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
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

    def _assert_install_untouched(self, inode, live) -> None:
        with open(os.path.join(self.install, "build-id.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "old-marker")
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["marker"], "KEEP-ME")
        self.assertEqual(os.stat(os.path.join(self.install, "data", "monitor.db")).st_ino, inode)
        with open(live, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "live\n")
        names = os.listdir(self.tmp)
        self.assertFalse(any(name.startswith("monitor-backup-") for name in names))
        self.assertFalse(any(name.startswith("monitor-failed-") for name in names))
        if os.path.isfile(self.syslog):
            with open(self.syslog, encoding="utf-8") as fh:
                self.assertNotIn("restart", fh.read())

    def test_check_only_passes_without_mutating_install(self) -> None:
        live = os.path.join(self.install, "data", "live-only.txt")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("live\n")
        db = os.path.join(self.install, "data", "monitor.db")
        inode = os.stat(db).st_ino
        self._package({"build-id.txt": "brand-new-marker\n", "tests/test_ok.py": _ok_test()})
        proc = self._run(["--check-only"])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("[ok] 新包测试通过", proc.stdout)
        self.assertIn("检查通过（未改动安装目录）", proc.stdout)
        self.assertIn(sys.executable, proc.stdout)
        self.assertNotIn("同步代码", proc.stdout)
        self.assertNotIn("备份到", proc.stdout)
        self._assert_install_untouched(inode, live)

    def test_check_only_failed_tests_do_not_mutate(self) -> None:
        live = os.path.join(self.install, "data", "live-only.txt")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("live\n")
        inode = os.stat(os.path.join(self.install, "data", "monitor.db")).st_ino
        self._package(
            {"build-id.txt": "brand-new-marker\n", "tests/test_boom.py": _boom_test()}
        )
        proc = self._run(["--check-only"])
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("未改动安装目录", proc.stdout)
        self.assertNotIn("检查通过", proc.stdout)
        self._assert_install_untouched(inode, live)

    def test_check_only_presync_does_not_read_open_stdin(self) -> None:
        """测试如果去读继承来的 stdin，管道不关就会一直堵住。"""
        reader = textwrap.dedent(
            """
            import sys
            import unittest

            class Stdin(unittest.TestCase):
                def test_stdin_is_devnull(self):
                    self.assertEqual(sys.stdin.read(1), "")
            """
        )
        live = os.path.join(self.install, "data", "live-only.txt")
        with open(live, "w", encoding="utf-8") as fh:
            fh.write("live\n")
        inode = os.stat(os.path.join(self.install, "data", "monitor.db")).st_ino
        self._package({"build-id.txt": "brand-new-marker\n", "tests/test_stdin.py": reader})
        proc = subprocess.Popen(
            [
                "bash",
                os.path.join(ROOT, "scripts", "upgrade_center.sh"),
                self.pkg,
                "--dir",
                self.install,
                "--sha",
                "abc123",
                "--check-only",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=self.base_env,
        )
        try:
            rc = proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            self.fail("pre-sync tests blocked on open stdin:\n" + out)
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        if proc.stdout:
            proc.stdout.close()
        if proc.stdin:
            proc.stdin.close()
        self.assertEqual(rc, 0, out)
        self.assertIn("检查通过（未改动安装目录）", out)
        self._assert_install_untouched(inode, live)

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
        self._upgrade_script = None

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _require_git(self) -> str:
        git = shutil.which("git")
        if not git:
            self.skipTest("本机没有 git，跳过 push_upgrade.sh 仓库测试")
        return git

    def _copy_push_scripts(self, repo: str) -> str:
        dest_dir = os.path.join(repo, "scripts", "lib")
        os.makedirs(dest_dir)
        script = os.path.join(repo, "scripts", "push_upgrade.sh")
        shutil.copy(os.path.join(ROOT, "scripts", "push_upgrade.sh"), script)
        shutil.copy(
            os.path.join(ROOT, "scripts", "lib", "ssh_cmd.sh"),
            os.path.join(dest_dir, "ssh_cmd.sh"),
        )
        return script

    def _upgrade_in_git_repo(self) -> str:
        """脚本按自身所在目录找仓库。git archive 解包没有 .git，所以拷到临时仓库再跑。"""
        if self._upgrade_script:
            return self._upgrade_script
        git = self._require_git()
        repo = os.path.join(self.tmp, "repo")
        os.makedirs(repo)
        script = self._copy_push_scripts(repo)
        git_env = dict(self.env)
        git_env.pop("MONITOR_PUSH_DRY", None)

        def git_ok(args):
            proc = subprocess.run(
                args,
                cwd=repo,
                env=git_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
            )
            if proc.returncode != 0:
                raise AssertionError(proc.stdout)
            return proc

        git_ok([git, "-c", "init.defaultBranch=master", "-c", "advice.defaultBranchName=false", "init", "-q"])
        git_ok(
            [
                git,
                "-c",
                "user.email=monitor-test@example.com",
                "-c",
                "user.name=monitor-test",
                "-c",
                "commit.gpgsign=false",
                "add",
                "scripts",
            ]
        )
        git_ok(
            [
                git,
                "-c",
                "user.email=monitor-test@example.com",
                "-c",
                "user.name=monitor-test",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                "test",
            ]
        )
        self._upgrade_script = script
        return script

    def _run_push(self, args, env):
        return _captured(["bash", self._upgrade_in_git_repo()] + list(args), env, 20)

    def test_sshpass_env_wraps_ssh(self) -> None:
        self.env["SSHPASS"] = "secret-token"
        proc = self._run_push(["root@center", "HEAD"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=sshpass -e ssh", proc.stdout)
        self.assertNotIn("secret-token", proc.stdout)
        self.assertRegex(proc.stdout, r"SHA=[0-9a-f]{7,40}")
        self.assertFalse(os.path.exists(self.log))

    def test_plain_ssh_without_sshpass_env(self) -> None:
        proc = self._run_push(["root@center", "HEAD"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=ssh\n", proc.stdout)
        self.assertNotIn("sshpass", proc.stdout)
        self.assertRegex(proc.stdout, r"SHA=[0-9a-f]{7,40}")

    def test_ssh_cmd_override_skips_sshpass(self) -> None:
        self.env["SSHPASS"] = "secret-token"
        proc = self._run_push(
            ["--ssh-cmd", "fake-ssh -o BatchMode=yes", "root@center", "HEAD"],
            self.env,
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
        proc = self._run_push(["root@center", "HEAD"], self.env)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("没有 sshpass", proc.stdout)

    def test_dry_run_outside_git_repo_does_not_fail(self) -> None:
        self._require_git()
        plain = os.path.join(self.tmp, "not-a-repo")
        script = self._copy_push_scripts(plain)
        proc = _captured(["bash", script, "root@center", "HEAD"], self.env, 20)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SHA=unknown", proc.stdout)
        self.assertNotIn("not a git repository", proc.stdout)
        self.assertIn("SSH=ssh\n", proc.stdout)

    def test_push_upgrade_plans_bundle_mkdir(self) -> None:
        bundle = os.path.join(self.tmp, "vendor")
        os.makedirs(bundle)
        with open(os.path.join(bundle, "cpython.tar.gz"), "wb") as fh:
            fh.write(b"bundle")
        self.env["MONITOR_PUSH_BUNDLE_DIR"] = bundle
        proc = self._run_push(["root@center", "HEAD"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("BUNDLES=sync", proc.stdout)
        self.assertIn("MKDIR=/opt/monitor/vendor/python", proc.stdout)

    @unittest.skipIf(
        sys.platform == "darwin",
        "macOS bsdtar 往管道里写 AppleDouble 扩展头时退出码和 Linux 不一致；推包已设置 COPYFILE_DISABLE=1，并在 tar 支持时加 --no-xattrs / --disable-copyfile。此用例在 Darwin 上跳过",
    )
    def test_push_bundles_mkdirs_missing_remote_dir(self) -> None:
        local = os.path.join(self.tmp, "local-bundles")
        os.makedirs(local)
        with open(os.path.join(local, "bundles.json"), "w", encoding="utf-8") as fh:
            fh.write("{}\n")
        with open(os.path.join(local, "cpython.tar.gz"), "wb") as fh:
            fh.write(b"bundle")
        env = dict(self.env)
        env.pop("MONITOR_PUSH_DRY", None)
        proc = _captured(
            [
                "bash",
                os.path.join(ROOT, "scripts", "push_python_bundles.sh"),
                "--ssh-cmd",
                os.path.join(self.bin, "fake-ssh"),
                "root@center",
                local,
            ],
            env,
            20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        with open(self.log, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("mkdir -p '/opt/monitor/vendor/python'", log)
        self.assertIn("tar -xf - -C '/opt/monitor/vendor/python'", log)
        mkdir_lines = [ln for ln in log.splitlines() if "mkdir -p" in ln]
        tar_lines = [ln for ln in log.splitlines() if "tar -xf -" in ln]
        self.assertTrue(mkdir_lines)
        self.assertTrue(all(ln.startswith("-n ") for ln in mkdir_lines), log)
        self.assertTrue(tar_lines)
        self.assertTrue(all(not ln.startswith("-n ") for ln in tar_lines), log)

    def test_check_only_dry_run_uses_ssh_n(self) -> None:
        proc = self._run_push(["--check-only", "root@center", "HEAD"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=ssh\n", proc.stdout)
        self.assertIn("CHECK_ONLY=1", proc.stdout)
        self.assertIn("UPGRADE=ssh -n", proc.stdout)
        self.assertIn("REMOTE=--check-only", proc.stdout)
        self.assertIn("BUNDLES=skip", proc.stdout)
        self.assertNotIn("sshpass", proc.stdout)

    def test_check_only_dry_run_sshpass_keeps_n_after_ssh(self) -> None:
        self.env["SSHPASS"] = "secret-token"
        proc = self._run_push(["--check-only", "root@center", "HEAD"], self.env)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("SSH=sshpass -e ssh", proc.stdout)
        self.assertIn("UPGRADE=sshpass -e ssh -n", proc.stdout)
        self.assertIn("REMOTE=--check-only", proc.stdout)
        self.assertNotIn("secret-token", proc.stdout)

    @unittest.skipIf(
        sys.platform == "darwin",
        "macOS bsdtar 往管道里写 AppleDouble 扩展头时退出码和 Linux 不一致；推包已设置 COPYFILE_DISABLE=1，并在 tar 支持时加 --no-xattrs / --disable-copyfile。此用例在 Darwin 上跳过",
    )
    def test_push_bundles_ignores_open_stdin(self) -> None:
        local = os.path.join(self.tmp, "local-bundles")
        os.makedirs(local)
        with open(os.path.join(local, "cpython.tar.gz"), "wb") as fh:
            fh.write(b"bundle")
        env = dict(self.env)
        env.pop("MONITOR_PUSH_DRY", None)
        proc = subprocess.Popen(
            [
                "bash",
                os.path.join(ROOT, "scripts", "push_python_bundles.sh"),
                "--ssh-cmd",
                os.path.join(self.bin, "fake-ssh"),
                "root@center",
                local,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            rc = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
            self.fail("bundle push blocked on open stdin:\n" + out)
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        if proc.stdout:
            proc.stdout.close()
        if proc.stdin:
            proc.stdin.close()
        self.assertEqual(rc, 0, out)
        with open(self.log, encoding="utf-8") as fh:
            log = fh.read()
        self.assertIn("mkdir -p '/opt/monitor/vendor/python'", log)
        self.assertTrue(
            any(ln.startswith("-n ") and "mkdir -p" in ln for ln in log.splitlines()),
            log,
        )
