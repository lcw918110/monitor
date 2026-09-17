#!/usr/bin/env python3
"""Agent / common / daemonize 的 Python 3.6 语法约束检查。

CI 在当前解释器上跑；本测试用源码扫描 + ast 确保采集端路径
不依赖 3.7+（future annotations）、3.8+（walrus 等）、3.9+ 内置泛型、
3.10+ 的 X|Y 注解，以及 subprocess 的 text=（3.7+）。
"""

import ast
import os
import shlex
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

AGENT_PATHS = (
    os.path.join(ROOT, "agent"),
    os.path.join(ROOT, "common"),
    os.path.join(ROOT, "scripts", "daemonize_run.py"),
)

_BUILTIN_GENERICS = {"list", "dict", "tuple", "set", "type", "frozenset"}


def _iter_py_files():
    for path in AGENT_PATHS:
        if os.path.isfile(path) and path.endswith(".py"):
            yield path
            continue
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def _source(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _annotation_has_bitor(node):
    if node is None:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.BinOp) and isinstance(child.op, ast.BitOr):
            return True
    return False


def _annotation_has_builtin_generic(node):
    if node is None:
        return False
    for child in ast.walk(node):
        if not isinstance(child, ast.Subscript):
            continue
        value = child.value
        name = None
        if isinstance(value, ast.Name):
            name = value.id
        if name in _BUILTIN_GENERICS:
            return True
    return False


def _iter_annotations(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for arg in node.args.args:
                yield arg.annotation
            if sys.version_info >= (3, 8):
                for arg in getattr(node.args, "posonlyargs", []) or []:
                    yield arg.annotation
            for arg in node.args.kwonlyargs:
                yield arg.annotation
            yield node.args.vararg.annotation if node.args.vararg else None
            yield node.args.kwarg.annotation if node.args.kwarg else None
            yield node.returns
        elif isinstance(node, ast.AnnAssign):
            yield node.annotation


class AgentPy36CompatTests(unittest.TestCase):
    def test_no_future_annotations(self):
        needle = "from __future__ import annotations"
        bad = []
        for path in _iter_py_files():
            src = _source(path)
            if needle in src:
                bad.append(os.path.relpath(path, ROOT))
        self.assertEqual(bad, [], "Agent/common 在 3.6 上不能使用 future annotations")

    def test_no_subprocess_text_kwarg(self):
        bad = []
        for path in _iter_py_files():
            src = _source(path)
            if "text=True" in src or "text = True" in src:
                bad.append(os.path.relpath(path, ROOT))
        self.assertEqual(
            bad,
            [],
            "subprocess text= 是 3.7+；3.6 请用 universal_newlines=True",
        )

    def test_ast_parses_as_python36(self):
        kwargs = {}
        if sys.version_info >= (3, 10):
            kwargs["feature_version"] = (3, 6)
        errors = []
        for path in _iter_py_files():
            src = _source(path)
            try:
                ast.parse(src, filename=path, **kwargs)
            except SyntaxError as exc:
                errors.append("%s: %s" % (os.path.relpath(path, ROOT), exc))
        self.assertEqual(errors, [])

    def test_no_py38_plus_syntax_nodes(self):
        """walrus / match / 仅限位置参数：3.8+。"""
        bad = []
        named_expr = getattr(ast, "NamedExpr", None)
        match_cls = getattr(ast, "Match", None)
        for path in _iter_py_files():
            tree = ast.parse(_source(path), filename=path)
            rel = os.path.relpath(path, ROOT)
            for node in ast.walk(tree):
                if named_expr and isinstance(node, named_expr):
                    bad.append("%s: walrus :=" % rel)
                if match_cls and isinstance(node, match_cls):
                    bad.append("%s: match/case" % rel)
                if isinstance(node, ast.FunctionDef):
                    if getattr(node.args, "posonlyargs", None):
                        bad.append("%s: positional-only /" % rel)
        self.assertEqual(bad, [])

    def test_annotations_are_typing_not_pep604_or_pep585(self):
        bad = []
        for path in _iter_py_files():
            tree = ast.parse(_source(path), filename=path)
            rel = os.path.relpath(path, ROOT)
            for ann in _iter_annotations(tree):
                if _annotation_has_bitor(ann):
                    bad.append("%s: X|Y union (use Optional/Union)" % rel)
                if _annotation_has_builtin_generic(ann):
                    bad.append("%s: list[T]/dict[K,V] (use typing.List/Dict)" % rel)
        self.assertEqual(bad, [])

    def test_agent_modules_import(self):
        from agent import main as agent_main
        from agent.metrics import accelerators, disk, gpu, network, npu, system
        from common import host_type, validate
        import importlib.util

        self.assertTrue(callable(agent_main.build_payload))
        self.assertTrue(callable(system.collect_system))
        self.assertTrue(callable(disk.collect_disks))
        self.assertTrue(callable(accelerators.collect_accelerators))
        self.assertTrue(callable(gpu.collect_gpus))
        self.assertTrue(callable(npu.collect_npus))
        self.assertTrue(callable(network.collect_network))
        self.assertEqual(host_type.normalize_host_type("npu"), "gpu")
        ok, errors = validate.validate_agent_config(
            {
                "center_url": "http://127.0.0.1:8080/api/v1/metrics",
                "interval_seconds": 15,
                "host_type": "auto",
            }
        )
        self.assertTrue(ok, errors)
        dpath = os.path.join(ROOT, "scripts", "daemonize_run.py")
        spec = importlib.util.spec_from_file_location("daemonize_run", dpath)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.become_daemon))


class ResolvePythonScriptTests(unittest.TestCase):
    """通过 bash source 验证「先复用、再安装」解析器。"""

    def setUp(self):
        self.lib = os.path.join(ROOT, "scripts", "lib", "resolve_python.sh")
        self.assertTrue(os.path.isfile(self.lib))

    def _run(self, body, extra_env=None):
        import subprocess

        env = os.environ.copy()
        env["MONITOR_INSTALL_PYTHON"] = "0"
        env["MONITOR_PYTHON_LOCAL_GLOBS"] = "/no/such/python3.*/bin/python3"
        env.pop("PYTHON_BIN", None)
        env.pop("MONITOR_PYTHON_SEARCH_PATH", None)
        if extra_env:
            env.update(extra_env)
        search = extra_env.get("MONITOR_PYTHON_SEARCH_PATH", "") if extra_env else ""
        search_line = ""
        if search:
            search_line = "MONITOR_PYTHON_SEARCH_PATH=%s" % shlex.quote(search)
        script = textwrap.dedent(
            """\
            set -euo pipefail
            . %s
            MONITOR_INSTALL_PYTHON=0
            MONITOR_PYTHON_LOCAL_GLOBS="/no/such/python3.*/bin/python3"
            %s
            %s
            """
            % (self.lib, search_line, body)
        )
        proc = subprocess.run(
            ["bash", "-s"],
            input=script,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            env=env,
            timeout=15,
        )
        return proc

    def _write_fake_python(self, directory, name, version, exit_code, need_ld=None):
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if need_ld:
            check = (
                'case ":${LD_LIBRARY_PATH:-}:" in\n'
                "  *:%s:*) ;;\n"
                '  *) echo "error while loading shared libraries: libpython" >&2; exit 127 ;;\n'
                "esac\n"
            ) % need_ld
        else:
            check = ""
        body = (
            "#!/bin/bash\n"
            "%s"
            'if [[ "${1:-}" == "-c" ]]; then\n'
            "  printf '%%s\\n' '%s'\n"
            "  exit %s\n"
            "fi\n"
            'echo "unexpected: $*" >&2\n'
            "exit 1\n"
        ) % (check, version, exit_code)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path

    def test_python_bin_preferred_if_ok(self):
        tmp = tempfile.mkdtemp()
        fake = self._write_fake_python(tmp, "my-python", "3.9", 0)
        proc = self._run(
            """
            resolve_python 3 6
            echo PY=$PY
            echo VER=$PY_VERSION
            """,
            extra_env={"PYTHON_BIN": fake, "MONITOR_PYTHON_SEARCH_PATH": tmp},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PY=" + fake, proc.stdout)
        self.assertIn("VER=3.9", proc.stdout)

    def test_stale_python_bin_falls_back(self):
        tmp = tempfile.mkdtemp()
        old = self._write_fake_python(tmp, "old", "3.5", 2)
        good = self._write_fake_python(tmp, "python3.12", "3.12", 0)
        proc = self._run(
            """
            resolve_python 3 6
            echo PY=$PY
            echo VER=$PY_VERSION
            """,
            extra_env={"PYTHON_BIN": old, "MONITOR_PYTHON_SEARCH_PATH": tmp},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PY=" + good, proc.stdout)
        self.assertIn("VER=3.12", proc.stdout)
        self.assertIn("不可用或版本低于", proc.stderr)

    def test_picks_highest_usable_version(self):
        tmp = tempfile.mkdtemp()
        self._write_fake_python(tmp, "python3.6", "3.6", 0)
        high = self._write_fake_python(tmp, "python3.11", "3.11", 0)
        self._write_fake_python(tmp, "python3", "3.6", 0)
        proc = self._run(
            """
            unset PYTHON_BIN || true
            resolve_python 3 6
            echo PY=$PY
            echo VER=$PY_VERSION
            """,
            extra_env={"MONITOR_PYTHON_SEARCH_PATH": tmp, "PYTHON_BIN": ""},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("PY=" + high, proc.stdout)
        self.assertIn("VER=3.11", proc.stdout)

    def test_sets_ld_library_path_for_custom_prefix(self):
        tmp = tempfile.mkdtemp()
        prefix = os.path.join(tmp, "python3.12")
        bindir = os.path.join(prefix, "bin")
        libdir = os.path.join(prefix, "lib")
        os.makedirs(bindir)
        os.makedirs(libdir)
        open(os.path.join(libdir, "libpython3.12.so"), "a").close()
        fake = self._write_fake_python(
            bindir, "python3", "3.12", 0, need_ld=libdir
        )
        proc = self._run(
            """
            resolve_python 3 6
            echo PY=$PY
            echo VER=$PY_VERSION
            echo LD=$PY_LD_LIBRARY_PATH
            """,
            extra_env={
                "PYTHON_BIN": fake,
                "MONITOR_PYTHON_SEARCH_PATH": tempfile.mkdtemp(),
            },
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertIn("PY=" + fake, proc.stdout)
        self.assertIn("LD=" + libdir, proc.stdout)

    def test_error_when_none_usable(self):
        tmp = tempfile.mkdtemp()
        self._write_fake_python(tmp, "python3", "3.5", 2)
        proc = self._run(
            """
            unset PYTHON_BIN || true
            if ensure_python 3 6; then
              echo unexpectedly_ok
              exit 0
            fi
            echo ENSURE_FAIL
            """,
            extra_env={"MONITOR_PYTHON_SEARCH_PATH": tmp, "PYTHON_BIN": ""},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ENSURE_FAIL", proc.stdout)
        self.assertIn("未找到可用的 Python >= 3.6", proc.stdout)

    def test_deploy_agent_sources_resolver(self):
        deploy = os.path.join(ROOT, "scripts", "deploy_agent.sh")
        with open(deploy, "r", encoding="utf-8") as f:
            src = f.read()
        self.assertIn("resolve_python.sh", src)
        self.assertIn("ensure_python 3 6", src)
        self.assertIn("MONITOR_INSTALL_PYTHON=1", src)
        self.assertNotIn('command -v python3 >/dev/null || { echo "需要 python3"', src)


if __name__ == "__main__":
    unittest.main()
