"""便携 Python：架构选择、校验、版本下限，以及安装失败后的回退。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from center.deploy_errors import failure_from_remote_output
from center.python_bundle import arch_key, select_bundle


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        digest.update(fh.read())
    return digest.hexdigest()


def _fake_bundle(directory: str, version: str = "3.10") -> str:
    stage = tempfile.mkdtemp(prefix="bundle-stage-")
    bindir = os.path.join(stage, "python", "bin")
    os.makedirs(bindir)
    path = os.path.join(bindir, "python3")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(
            "#!/bin/bash\n"
            'if [[ "${1:-}" == "-c" ]]; then\n'
            "  printf '%%s\\n' '%s'\n"
            "  exit 0\n"
            "fi\n"
            "exit 1\n" % version
        )
    os.chmod(path, 0o755)
    archive = os.path.join(directory, "bundle.tar.gz")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(os.path.join(stage, "python"), arcname="python")
    shutil.rmtree(stage)
    return archive


class SelectBundleTests(unittest.TestCase):
    def test_pinned_manifest_hashes(self) -> None:
        with open(os.path.join(ROOT, "vendor", "python", "bundles.json"), encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertEqual(
            data["arches"]["x86_64"]["sha256"],
            "3db2171e03c1a7acdc599fba583c1b92306d3788b375c9323077367af1e9d9de",
        )
        self.assertEqual(
            data["arches"]["aarch64"]["sha256"],
            "eb58581f85fde83d1f3e8e1f8c6f5a15c7ae4fdbe3b1d1083931f9167fdd8dbc",
        )
        self.assertIn("install_only", data["arches"]["x86_64"]["filename"])
        self.assertNotIn("musl", data["arches"]["x86_64"]["filename"])
        self.assertEqual(arch_key("amd64"), "x86_64")
        self.assertEqual(arch_key("arm64"), "aarch64")
        self.assertEqual(arch_key("ppc64le"), "")

    def _manifest(self, directory: str, filename: str, sha: str) -> None:
        payload = {
            "arches": {
                "x86_64": {
                    "triple": "x86_64-unknown-linux-gnu",
                    "filename": filename,
                    "sha256": sha,
                }
            }
        }
        with open(os.path.join(directory, "bundles.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)

    def test_arch_missing_and_checksum(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        blob = os.path.join(tmp, "py.tar.gz")
        with open(blob, "wb") as fh:
            fh.write(b"not-a-real-tar")
        sha = _sha256(blob)
        self._manifest(tmp, "py.tar.gz", sha)
        info, code, _msg = select_bundle("x86_64", tmp)
        self.assertIsNone(code)
        self.assertEqual(info["sha256"], sha)
        info, code, msg = select_bundle("mips", tmp)
        self.assertIsNone(info)
        self.assertEqual(code, "python_bundle_arch")
        self.assertIn("mips", msg)
        info, code, _msg = select_bundle("aarch64", tmp)
        self.assertEqual(code, "python_bundle_arch")
        os.remove(blob)
        info, code, msg = select_bundle("x86_64", tmp)
        self.assertEqual(code, "python_bundle_missing")
        self.assertIn("py.tar.gz", msg)
        with open(blob, "wb") as fh:
            fh.write(b"tampered")
        info, code, msg = select_bundle("x86_64", tmp)
        self.assertEqual(code, "python_bundle_checksum")
        self.assertIn("校验失败", msg)

    def test_fail_line_beats_python_install_substring(self) -> None:
        text = (
            "[warn] python_install_fail: apt 安装 python3 失败\n"
            "[fail] python_bundle_missing: 中心未放置 x86_64 包\n"
        )
        code, msg = failure_from_remote_output(text)
        self.assertEqual(code, "python_bundle_missing")
        self.assertIn("未放置", msg)


class BundleShellTests(unittest.TestCase):
    def _run(self, body: str, env=None) -> subprocess.CompletedProcess:
        script = textwrap.dedent(
            """
            set -euo pipefail
            . "{root}/scripts/lib/python_bundle.sh"
            {body}
            """
        ).format(root=ROOT, body=textwrap.dedent(body).strip("\n"))
        # 默认隔离宿主机解释器：/usr/local/python3.8 这类 glob 和 PATH 里的 python 都不能进来。
        merged = dict(os.environ)
        merged.pop("PYTHON_BIN", None)
        merged.pop("PYTHONPATH", None)
        merged.pop("PYTHONHOME", None)
        merged["PATH"] = "/usr/bin:/bin"
        merged["MONITOR_PYTHON_LOCAL_GLOBS"] = "/no/such/python3.*/bin/python3"
        merged["MONITOR_INSTALL_PYTHON"] = "0"
        merged.update(env or {})
        return subprocess.run(
            ["bash", "-s"],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=merged,
            timeout=20,
        )

    def _write_fake(self, directory, name, version, exit_code):
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/bash\n"
                'if [[ "${1:-}" == "-c" ]]; then\n'
                "  printf '%%s\\n' '%s'\n"
                "  exit %s\n"
                "fi\n"
                "exit 1\n" % (version, exit_code)
            )
        os.chmod(path, 0o755)
        return path

    def test_resolver_picks_highest_and_rejects_35(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self._write_fake(tmp, "python3.6", "3.6", 0)
        self._write_fake(tmp, "python3.10", "3.10", 0)
        self._write_fake(tmp, "python3", "3.5", 2)
        proc = self._run(
            """
            unset PYTHON_BIN || true
            resolve_python "$MONITOR_AGENT_PY_MIN_MAJOR" "$MONITOR_AGENT_PY_MIN_MINOR"
            echo "PY=$(basename "$PY")"
            echo "VER=$PY_VERSION"
            """,
            env={"MONITOR_PYTHON_SEARCH_PATH": tmp, "PYTHON_BIN": ""},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PY=python3.10", proc.stdout)
        self.assertIn("VER=3.10", proc.stdout)

        only = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, only, True)
        self._write_fake(only, "python3", "3.5", 2)
        proc = self._run(
            """
            unset PYTHON_BIN || true
            if resolve_python 3 6; then
              echo unexpectedly_ok
              exit 1
            fi
            echo REJECTED
            """,
            env={"MONITOR_PYTHON_SEARCH_PATH": only, "PYTHON_BIN": ""},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("REJECTED", proc.stdout)

    def test_run_ignores_host_path_and_local_python(self) -> None:
        decoy = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, decoy, True)
        self._write_fake(decoy, "python3.12", "3.12", 0)
        self._write_fake(decoy, "python3", "3.12", 0)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = decoy + os.pathsep + old_path
        try:
            only = tempfile.mkdtemp()
            self.addCleanup(shutil.rmtree, only, True)
            self._write_fake(only, "python3.6", "3.6", 0)
            proc = self._run(
                """
                echo "GLOBS=$MONITOR_PYTHON_LOCAL_GLOBS"
                case ":$PATH:" in
                  *:%s:*) echo PATH_LEAKED; exit 1 ;;
                esac
                unset PYTHON_BIN || true
                resolve_python 3 6
                echo "PY=$(basename "$PY")"
                echo "VER=$PY_VERSION"
                """
                % decoy,
                env={"MONITOR_PYTHON_SEARCH_PATH": only, "PYTHON_BIN": ""},
            )
        finally:
            if old_path:
                os.environ["PATH"] = old_path
            else:
                os.environ.pop("PATH", None)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("GLOBS=/no/such/python3.*/bin/python3", proc.stdout)
        self.assertNotIn("PATH_LEAKED", proc.stdout)
        self.assertIn("PY=python3.6", proc.stdout)
        self.assertIn("VER=3.6", proc.stdout)

    def test_dest_root_versus_install_dir(self) -> None:
        proc = self._run(
            """
            MONITOR_PYTHON_ASSUME_ROOT=1
            echo "ROOTDEST=$(python_bundle_dest)"
            MONITOR_PYTHON_ASSUME_ROOT=0
            INSTALL_DIR=/tmp/monitor-agent-example
            echo "USERDEST=$(python_bundle_dest)"
            """
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("ROOTDEST=/usr/local/monitor-python", proc.stdout)
        self.assertIn("USERDEST=/tmp/monitor-agent-example/python", proc.stdout)

    def test_checksum_failure_and_fallback_after_install_fail(self) -> None:
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        archive = _fake_bundle(tmp, "3.10")
        sha = _sha256(archive)
        dest = os.path.join(tmp, "extracted")
        proc = self._run(
            """
            install_bundled_python 3 6 || true
            echo "CODE=$MONITOR_PYTHON_BUNDLE_CODE"
            """,
            env={
                "PYTHON_BUNDLE": archive,
                "PYTHON_BUNDLE_SHA256": "0" * 64,
                "MONITOR_PYTHON_BUNDLE_DEST": dest,
                "MONITOR_PYTHON_ASSUME_ROOT": "0",
            },
        )
        self.assertIn("CODE=python_bundle_checksum", proc.stdout)
        self.assertFalse(os.path.isdir(dest))

        empty = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, empty, True)
        self._write_fake(empty, "python3", "3.5", 2)
        proc = self._run(
            """
            unset PYTHON_BIN || true
            if ! ensure_agent_python 3 6; then
              echo "FALLBACK_FAIL=$MONITOR_PYTHON_BUNDLE_CODE"
              exit 1
            fi
            echo "PY=$PY"
            echo "VER=$PY_VERSION"
            test -x "$MONITOR_PYTHON_BUNDLE_DEST/bin/python3"
            """,
            env={
                "MONITOR_PYTHON_SEARCH_PATH": empty,
                "PYTHON_BIN": "",
                "MONITOR_INSTALL_PYTHON": "1",
                "MONITOR_PYTHON_INSTALL_STUB": "apt 失败",
                "PYTHON_BUNDLE": archive,
                "PYTHON_BUNDLE_SHA256": sha,
                "MONITOR_PYTHON_BUNDLE_DEST": dest,
                "MONITOR_PYTHON_ASSUME_ROOT": "0",
                "INSTALL_DIR": os.path.join(tmp, "agent"),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("VER=3.10", proc.stdout)
        self.assertIn(os.path.join(dest, "bin", "python3"), proc.stdout)
        self.assertIn("python_install_fail", proc.stdout + proc.stderr)

    def test_fetch_dry_run_uses_pinned_hash(self) -> None:
        proc = subprocess.run(
            ["bash", os.path.join(ROOT, "scripts", "fetch_python_bundle.sh")],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            env=dict(os.environ, MONITOR_FETCH_PYTHON_DRY="1"),
            timeout=20,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn(
            "3db2171e03c1a7acdc599fba583c1b92306d3788b375c9323077367af1e9d9de",
            proc.stdout,
        )
        self.assertIn("aarch64-unknown-linux-gnu-install_only", proc.stdout)

    def test_scripts_parse(self) -> None:
        for name in (
            "scripts/fetch_python_bundle.sh",
            "scripts/upgrade_center.sh",
            "scripts/push_upgrade.sh",
            "scripts/push_python_bundles.sh",
            "scripts/lib/python_bundle.sh",
            "scripts/lib/ssh_cmd.sh",
            "scripts/deploy_agent.sh",
            "scripts/deploy_fleet.sh",
        ):
            proc = subprocess.run(
                ["bash", "-n", os.path.join(ROOT, name)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
            )
            self.assertEqual(proc.returncode, 0, name + "\n" + proc.stdout)
        with open(os.path.join(ROOT, "scripts", "deploy_agent.sh"), encoding="utf-8") as fh:
            src = fh.read()
        bundle_at = src.index("MONITOR_PYTHON_BUNDLE_CODE")
        install_at = src.index("python_install_fail", bundle_at)
        self.assertLess(bundle_at, install_at)
        with open(os.path.join(ROOT, "scripts", "push_upgrade.sh"), encoding="utf-8") as fh:
            push = fh.read()
        self.assertIn("$REMOTE_DIR/scripts/upgrade_center.sh", push)
        self.assertNotIn("/opt/monitor/scripts/upgrade_center.sh", push)
        self.assertIn("git archive", push)
        self.assertIn("sshpass -e ssh", push)
        self.assertIn("monitor_push_bundles", push)
