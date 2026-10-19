"""选择中心上已经放好的便携 CPython。这里不下载。

包是 python-build-standalone 的 install_only（glibc >= 2.17），按目标机
``uname -m`` 选。校验和以 ``vendor/python/bundles.json`` 为准。
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Optional, Tuple

ARCH_ALIASES = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
}

BundleInfo = Dict[str, str]


def project_manifest_path(project_root: str) -> str:
    return os.path.join(project_root, "vendor", "python", "bundles.json")


def load_manifest(directory: str) -> Dict[str, Any]:
    path = os.path.join(directory, "bundles.json")
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, dict) else {}


def default_bundles_dir(project_root: str) -> str:
    opt = "/opt/monitor/vendor/python"
    if os.path.isdir(opt):
        return opt
    return os.path.join(project_root, "vendor", "python")


def bundles_dir_from_config(config: Optional[Dict[str, Any]], project_root: str) -> str:
    env = os.environ.get("MONITOR_PYTHON_BUNDLES", "").strip()
    if env:
        return env
    deploy = {}
    if isinstance(config, dict) and isinstance(config.get("deploy"), dict):
        deploy = config["deploy"]
    configured = str(deploy.get("python_bundles") or "").strip()
    if configured:
        return configured
    return default_bundles_dir(project_root)


def arch_key(uname_m: str) -> str:
    return ARCH_ALIASES.get((uname_m or "").strip().lower(), "")


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def select_bundle(
    uname_m: str, directory: str
) -> Tuple[Optional[BundleInfo], Optional[str], str]:
    """返回 ``(info, error_code, message)``。成功时 error_code 为 None。"""
    key = arch_key(uname_m)
    if not key:
        shown = (uname_m or "").strip() or "空"
        return None, "python_bundle_arch", "没有适合架构 %s 的便携 Python" % shown
    manifest = load_manifest(directory)
    arches = manifest.get("arches") if isinstance(manifest.get("arches"), dict) else {}
    spec = arches.get(key) if isinstance(arches.get(key), dict) else None
    if not spec:
        return (
            None,
            "python_bundle_arch",
            "清单里没有架构 %s 的便携 Python" % key,
        )
    filename = str(spec.get("filename") or "").strip()
    expect = str(spec.get("sha256") or "").strip().lower()
    if not filename or not expect:
        return None, "python_bundle_missing", "便携 Python 清单不完整（%s）" % key
    path = os.path.join(directory, filename)
    if not os.path.isfile(path):
        return (
            None,
            "python_bundle_missing",
            "中心未放置 %s（目录 %s）" % (filename, directory),
        )
    try:
        got = file_sha256(path)
    except OSError as exc:
        return None, "python_bundle_checksum", "无法读取便携 Python：%s" % exc
    if got != expect:
        return (
            None,
            "python_bundle_checksum",
            "便携 Python 校验失败（期望 %s，实际 %s）" % (expect, got),
        )
    info: BundleInfo = {
        "path": path,
        "sha256": expect,
        "filename": filename,
        "triple": str(spec.get("triple") or ""),
        "arch": key,
    }
    return info, None, ""
