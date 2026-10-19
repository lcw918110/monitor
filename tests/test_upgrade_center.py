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

    def test_failed_tests_roll_back_code_and_unit(self) -> None:
        self._package(
            {"build-id.txt": "brand-new-marker\n", "tests/test_boom.py": _boom_test()}
        )
        proc = self._run(None)
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("回滚", proc.stdout)
        with open(os.path.join(self.install, "build-id.txt"), encoding="utf-8") as fh:
            self.assertEqual(fh.read().strip(), "old-marker")
        with open(os.path.join(self.install, "config", "center.json"), encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["marker"], "KEEP-ME")
        with open(self.unit, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.original_unit)
        self.assertTrue(os.path.isfile(os.path.join(self.install, "tests", "test_ok.py")))
        self.assertFalse(os.path.isfile(os.path.join(self.install, "tests", "test_boom.py")))
        with open(os.path.join(self.install, "vendor", "python", "already.tar.gz"), encoding="utf-8") as fh:
            self.assertIn("bundle-bytes", fh.read())
