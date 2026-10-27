#!/usr/bin/env python3
"""中心备份与恢复。

与 scripts/upgrade_center.sh 使用同一目录布局::

    <安装目录的父目录>/<安装名>-backup-YYYYMMDDHHMMSS/
        code/                         安装树副本
        code/config/center.json
        code/data/monitor.db          sqlite3 backup API + quick_check
        code/vendor/                  含已放置的便携包（若安装树里有）
        monitor-center.service        单元文件副本（含 Environment=）
        backup-manifest.json          本脚本写入；升级产生的备份可以没有它

这不是升级，也不是双中心。换代码仍只走 scripts/push_upgrade.sh。
升级失败时的自动回滚只恢复代码和单元，不覆盖 data/。
要把数据库也写回去，必须显式 --with-data，并且中心进程已经停掉。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BACKUP_KIND = "monitor-center-backup"
BACKUP_NAME = re.compile(r"^.+-backup-.+$")
STAMP_RE = re.compile(r"^\d{14}$")
DEFAULT_UNIT = "/etc/systemd/system/monitor-center.service"
FORBIDDEN_INSTALL_DIRS = {
    "/",
    "/opt",
    "/usr",
    "/etc",
    "/home",
    "/root",
    "/var",
    "/tmp",
    "/private",
    "/private/tmp",
}


class BackupError(Exception):
    """操作员能看懂的拒绝原因。"""


def _real(path: str) -> str:
    return os.path.realpath(path)


def is_within(path: str, parent: str) -> bool:
    """path 等于 parent，或在 parent 里面。比较前做 realpath。"""
    child = _real(path)
    root = _real(parent)
    return child == root or child.startswith(root + os.sep)


def assert_install_dir(path: str) -> str:
    if not path or not os.path.isdir(path):
        raise BackupError("安装目录不存在: %s" % path)
    install = _real(path)
    if install in FORBIDDEN_INSTALL_DIRS:
        raise BackupError("拒绝把 %s 当作中心安装目录" % install)
    cfg = os.path.join(install, "config", "center.json")
    if not os.path.isfile(cfg):
        raise BackupError("缺少 %s ，拒绝备份或恢复以免对着错误目录操作" % cfg)
    return install


def assert_backup_dir(path: str) -> str:
    if not path or not os.path.isdir(path):
        raise BackupError("备份目录不存在: %s" % path)
    backup = _real(path)
    name = os.path.basename(backup)
    if not BACKUP_NAME.match(name):
        raise BackupError(
            "不是中心备份目录（名字需要是 <安装名>-backup-<时间>）: %s" % name
        )
    cfg = os.path.join(backup, "code", "config", "center.json")
    if not os.path.isfile(cfg):
        raise BackupError("备份里没有 code/config/center.json: %s" % backup)
    return backup


def backup_dir_for(install_dir: str, stamp: str, parent: Optional[str] = None) -> str:
    if not STAMP_RE.match(stamp):
        raise BackupError("备份时间戳必须是 14 位数字 YYYYMMDDHHMMSS")
    install = assert_install_dir(install_dir)
    base = os.path.basename(install)
    dest_parent = _real(parent) if parent else os.path.dirname(install)
    if not os.path.isdir(dest_parent):
        raise BackupError("备份父目录不存在: %s" % dest_parent)
    if is_within(dest_parent, install):
        raise BackupError("备份不能放进安装目录里面: %s" % dest_parent)
    return os.path.join(dest_parent, "%s-backup-%s" % (base, stamp))


def quick_check(db_path: str) -> None:
    if not os.path.isfile(db_path):
        raise BackupError("数据库不存在: %s" % db_path)
    try:
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise BackupError("quick_check 失败: %s" % exc) from exc
    if not row or str(row[0]).lower() != "ok":
        raise BackupError("quick_check 失败: %s" % (row,))


def sqlite_online_backup(src: str, dst: str) -> None:
    """用 sqlite3 backup API 做一致副本，再 quick_check。不调用 rsync。"""
    if not os.path.isfile(src):
        raise BackupError("数据库不存在: %s" % src)
    if os.path.exists(dst) and _real(src) == _real(dst):
        raise BackupError("拒绝把数据库备份到它自己: %s" % src)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        src_conn = sqlite3.connect(src)
        dst_conn = sqlite3.connect(dst)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
    except sqlite3.Error as exc:
        raise BackupError("数据库备份失败: %s" % exc) from exc
    quick_check(dst)


def db_counts(db_path: str) -> Dict[str, int]:
    counts = {"hosts": 0, "metrics_history": 0, "diagnostic_reports": 0}
    if not os.path.isfile(db_path):
        return counts
    conn = sqlite3.connect(db_path)
    try:
        for key in list(counts):
            try:
                counts[key] = int(conn.execute("SELECT COUNT(*) FROM %s" % key).fetchone()[0])
            except sqlite3.Error:
                counts[key] = 0
    finally:
        conn.close()
    return counts


def describe_restore(mode: str) -> Dict[str, Any]:
    """写明这一档会写回什么。升级自动回滚对应 code-only。"""
    if mode == "code-only":
        return {
            "mode": "code-only",
            "restores": [
                "代码",
                "config/center.json",
                "vendor/",
                "monitor-center.service（备份里有才写回）",
            ],
            "leaves_untouched": ["data/（含 data/monitor.db）"],
            "overwrites_database": False,
        }
    if mode == "with-data":
        return {
            "mode": "with-data",
            "restores": [
                "代码",
                "config/center.json",
                "vendor/",
                "monitor-center.service（备份里有才写回）",
                "data/monitor.db（sqlite backup API）",
            ],
            "leaves_untouched": ["data/ 里除 monitor.db 及其 wal/shm 以外的其他文件"],
            "overwrites_database": True,
        }
    raise BackupError("恢复模式只能是 code-only 或 with-data")


def _remove_path(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
        return
    shutil.rmtree(path)


def _replace_tree_except_data(src: str, dest: str) -> None:
    """用备份里的代码盖回安装目录。不删除、不覆盖 dest/data。"""
    data_dir = os.path.join(dest, "data")
    for name in os.listdir(dest):
        if name == "data":
            continue
        path = os.path.join(dest, name)
        if os.path.lexists(data_dir) and (is_within(path, data_dir) or _real(path) == _real(data_dir)):
            raise BackupError("拒绝删除现场数据: %s" % path)
        _remove_path(path)
    for name in os.listdir(src):
        if name == "data":
            continue
        src_item = os.path.join(src, name)
        dst_item = os.path.join(dest, name)
        if os.path.islink(src_item):
            link = os.readlink(src_item)
            os.symlink(link, dst_item)
        elif os.path.isdir(src_item):
            shutil.copytree(src_item, dst_item, symlinks=True)
        else:
            shutil.copy2(src_item, dst_item)


def _db_bytes(path: str) -> int:
    if os.path.isfile(path):
        return os.path.getsize(path)
    return 0


def _ensure_space(dest_parent: str, db_path: str) -> None:
    """与升级脚本同一口径：大约 3 倍数据库再加 20MB。不够就还没创建备份目录就退出。"""
    need = _db_bytes(db_path) * 3 + 20 * 1024 * 1024
    free = shutil.disk_usage(dest_parent).free
    if free < need:
        raise BackupError(
            "磁盘空间不足: 可用 %d 字节，至少需要 %d（约 3 倍数据库再加 20MB）"
            % (free, need)
        )


def _drop_copied_db(code_dir: str) -> None:
    db = os.path.join(code_dir, "data", "monitor.db")
    for suffix in ("", "-wal", "-shm"):
        path = db + suffix
        if os.path.lexists(path) and (os.path.islink(path) or os.path.isfile(path)):
            os.remove(path)


def create_backup(
    install_dir: str,
    stamp: Optional[str] = None,
    parent: Optional[str] = None,
    unit_path: Optional[str] = None,
) -> Dict[str, Any]:
    """复制安装树，再用 sqlite backup API 换掉副本里的数据库。不修改现场目录。"""
    install = assert_install_dir(install_dir)
    stamp = stamp or time.strftime("%Y%m%d%H%M%S", time.localtime())
    dest = backup_dir_for(install, stamp, parent=parent)
    if os.path.exists(dest):
        dest = dest + "-%d" % os.getpid()
    if os.path.exists(dest):
        raise BackupError("备份目录已存在: %s" % dest)
    db_live = os.path.join(install, "data", "monitor.db")
    _ensure_space(os.path.dirname(dest), db_live)
    os.makedirs(dest, exist_ok=False)
    code_dir = os.path.join(dest, "code")
    try:
        shutil.copytree(install, code_dir, symlinks=True)
        has_db = os.path.isfile(db_live)
        if has_db:
            _drop_copied_db(code_dir)
            sqlite_online_backup(db_live, os.path.join(code_dir, "data", "monitor.db"))
        unit_copied = False
        if unit_path and os.path.isfile(unit_path):
            shutil.copy2(unit_path, os.path.join(dest, "monitor-center.service"))
            unit_copied = True
        vendor = os.path.join(code_dir, "vendor")
        manifest = {
            "kind": BACKUP_KIND,
            "version": 1,
            "created_at": stamp,
            "install_dir": install,
            "backup_dir": dest,
            "preserved": {
                "config_center_json": True,
                "database": has_db,
                "vendor": os.path.isdir(vendor),
                "unit_file": unit_copied,
            },
            "counts": db_counts(os.path.join(code_dir, "data", "monitor.db")) if has_db else db_counts(""),
            "notes": [
                "与 upgrade_center.sh 的 <安装名>-backup-<时间> 是同一布局。",
                "这不是双中心，也不能代替 scripts/push_upgrade.sh。",
                "升级失败时的自动回滚只恢复代码和单元，不覆盖 data/。",
                "这份备份里的数据库要 scripts/restore_center.sh --with-data 才写回。",
            ],
        }
        with open(os.path.join(dest, "backup-manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
    except Exception:
        if os.path.isdir(dest) and not is_within(dest, install):
            shutil.rmtree(dest, ignore_errors=True)
        raise
    manifest["backup_dir"] = dest
    return manifest


def ordered_backup_dirs(base: str, paths: List[str]) -> List[str]:
    """排序委托给 scripts/lib/backup_prune.sh，和升级清理用同一规则。"""
    if not paths:
        return []
    lib = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib", "backup_prune.sh")
    proc = subprocess.run(
        [
            "bash",
            "-c",
            '. "$1" || exit 1; shift; base="$1"; shift; backup_paths_oldest_first "$base" "$@"',
            "bash",
            lib,
            base,
            *paths,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        raise BackupError("无法排序备份目录: %s" % (proc.stderr or proc.stdout).strip())
    ordered = [line for line in proc.stdout.splitlines() if line.strip()]
    if sorted(os.path.realpath(p) for p in ordered) != sorted(os.path.realpath(p) for p in paths):
        raise BackupError("备份排序结果和输入不一致，未删除任何目录")
    return ordered


def _assert_safe_to_delete(path: str, install: str, base: str) -> None:
    if os.path.islink(path):
        raise BackupError("拒绝删除符号链接: %s" % path)
    if not os.path.isdir(path):
        raise BackupError("拒绝删除非目录: %s" % path)
    name = os.path.basename(_real(path))
    if not name.startswith(base + "-backup-"):
        raise BackupError("拒绝删除非备份目录: %s" % path)
    if is_within(path, install) or is_within(install, path):
        raise BackupError("拒绝删除现场安装: %s" % path)
    data = os.path.join(install, "data")
    if os.path.exists(data) and (is_within(path, data) or is_within(data, path)):
        raise BackupError("拒绝删除现场数据: %s" % path)


def prune_backups(install_dir: str, keep: int) -> List[str]:
    """留下最新的 keep 份 <安装名>-backup-*。keep < 1 直接拒绝，避免一次删光。"""
    if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
        raise BackupError("--keep 至少为 1。不要用 0 把备份全部删掉。")
    install = assert_install_dir(install_dir)
    parent = os.path.dirname(install)
    base = os.path.basename(install)
    prefix = base + "-backup-"
    paths = []
    for name in os.listdir(parent):
        if not name.startswith(prefix):
            continue
        path = os.path.join(parent, name)
        if os.path.isdir(path) and not os.path.islink(path):
            paths.append(path)
    ordered = ordered_backup_dirs(base, paths)
    removed = []
    for path in ordered[: max(0, len(ordered) - keep)]:
        _assert_safe_to_delete(path, install, base)
        shutil.rmtree(path)
        removed.append(path)
    return removed


def restore_center(
    install_dir: str,
    backup_dir: str,
    mode: str,
    yes: bool = False,
    assume_stopped: bool = False,
    unit_path: Optional[str] = None,
) -> Dict[str, Any]:
    plan = describe_restore(mode)
    if not yes:
        raise BackupError("恢复会改写安装目录，需要 --yes")
    if mode == "with-data" and not assume_stopped:
        raise BackupError(
            "恢复数据库前必须先停掉 monitor-center，并传入 --assume-stopped。"
            "未停进程时直接换库会和 WAL 打架。"
        )
    install = assert_install_dir(install_dir)
    backup = assert_backup_dir(backup_dir)
    if is_within(install, backup) or is_within(backup, install):
        raise BackupError("备份目录和安装目录不能互相包含")
    src_code = os.path.join(backup, "code")
    db_src = os.path.join(src_code, "data", "monitor.db")
    if mode == "with-data":
        if not os.path.isfile(db_src):
            raise BackupError("备份里没有 data/monitor.db，不能 --with-data")
        quick_check(db_src)
    unit_src = os.path.join(backup, "monitor-center.service")
    # 不传 unit_path 就不写单元，避免在非 root 的测试或笔记本上碰 /etc。
    unit_dest = unit_path or None
    if os.path.isfile(unit_src) and unit_dest:
        parent = os.path.dirname(unit_dest) or "."
        if not os.path.isdir(parent) or not os.access(parent, os.W_OK):
            raise BackupError("单元文件目标不可写，未改动安装目录: %s" % unit_dest)
    _replace_tree_except_data(src_code, install)
    unit_restored = False
    if os.path.isfile(unit_src) and unit_dest:
        os.makedirs(os.path.dirname(unit_dest), exist_ok=True)
        shutil.copy2(unit_src, unit_dest)
        unit_restored = True
    db_restored = False
    if mode == "with-data":
        db_dst = os.path.join(install, "data", "monitor.db")
        os.makedirs(os.path.dirname(db_dst), exist_ok=True)
        for suffix in ("-wal", "-shm"):
            side = db_dst + suffix
            if not os.path.lexists(side):
                continue
            if os.path.islink(side) or os.path.isfile(side):
                os.remove(side)
            else:
                raise BackupError("拒绝删除非文件的数据库附属路径: %s" % side)
        sqlite_online_backup(db_src, db_dst)
        db_restored = True
    return {
        "ok": True,
        "mode": mode,
        "install_dir": install,
        "backup_dir": backup,
        "plan": plan,
        "unit_restored": unit_restored,
        "database_restored": db_restored,
        "counts": db_counts(os.path.join(install, "data", "monitor.db")),
    }


def _systemctl(args: List[str]) -> int:
    if os.environ.get("MONITOR_TEST_SANDBOX") == "1":
        print("[sandbox] 拒绝调用真实 systemctl", file=sys.stderr)
        return 1
    if shutil.which("systemctl") is None:
        return 127
    proc = subprocess.run(["systemctl", *args])
    return proc.returncode


def _print_manifest(manifest: Dict[str, Any]) -> None:
    preserved = manifest.get("preserved") or {}
    print("备份完成: %s" % manifest["backup_dir"])
    print("  center.json: %s" % ("在 code/config/center.json" if preserved.get("config_center_json") else "缺失"))
    print("  data/monitor.db: %s" % ("已做一致副本并 quick_check" if preserved.get("database") else "现场没有数据库"))
    print("  vendor/: %s" % ("已复制" if preserved.get("vendor") else "现场没有 vendor/"))
    print("  单元文件: %s" % ("已复制 monitor-center.service" if preserved.get("unit_file") else "未复制（现场没有该文件）"))
    counts = manifest.get("counts") or {}
    print(
        "  条数: 主机=%s 历史上报=%s 诊断报告=%s"
        % (counts.get("hosts", 0), counts.get("metrics_history", 0), counts.get("diagnostic_reports", 0))
    )
    print("这不是双中心。升级仍只用 scripts/push_upgrade.sh。")
    print("下一轮升级默认只保留最新 5 份 <安装名>-backup-*，其中也包括这次备份。")


def cmd_backup(args: argparse.Namespace) -> int:
    unit = None if args.no_unit else args.unit
    if unit is None and not args.no_unit and os.path.isfile(DEFAULT_UNIT):
        unit = DEFAULT_UNIT
    manifest = create_backup(args.dir, parent=args.parent, unit_path=unit)
    _print_manifest(manifest)
    if args.keep is not None:
        removed = prune_backups(args.dir, args.keep)
        for path in removed:
            print("[backup] 删除旧备份 %s" % path)
        print("保留最新 %d 份（与升级脚本认的是同一批目录名）" % args.keep)
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    if args.code_only == args.with_data:
        print("必须且只能指定 --code-only 或 --with-data 其中一个", file=sys.stderr)
        return 2
    mode = "with-data" if args.with_data else "code-only"
    assume = args.assume_stopped
    if mode == "with-data" and not assume and not args.no_systemd:
        rc = _systemctl(["stop", "monitor-center"])
        if rc != 0:
            print(
                "停 monitor-center 失败。确认进程已经停掉后加 --assume-stopped；"
                "没有 systemd 时同时加 --no-systemd --assume-stopped。未改动安装目录。",
                file=sys.stderr,
            )
            return 1
        assume = True
    unit = None if args.no_unit else (args.unit or DEFAULT_UNIT)
    result = restore_center(
        args.dir,
        args.from_dir,
        mode,
        yes=args.yes,
        assume_stopped=assume,
        unit_path=unit,
    )
    plan = result["plan"]
    print("恢复完成: %s" % mode)
    print("  安装目录: %s" % result["install_dir"])
    print("  来源: %s" % result["backup_dir"])
    print("  写回: %s" % "、".join(plan["restores"]))
    print("  不动: %s" % "、".join(plan["leaves_untouched"]))
    if result["unit_restored"]:
        print("  单元文件已写回（含备份当时的 Environment=）")
    else:
        print("  未写回单元文件")
    if mode == "code-only":
        print("  data/ 未改。这和升级失败时的自动回滚一样，不会用备份覆盖数据库。")
    else:
        print("  data/monitor.db 已用备份里的一致副本覆盖。备份之后写入的历史上报不在这份库里。")
    print("这不是双中心，也没有自动故障切换。")
    if not args.no_systemd:
        reload_rc = _systemctl(["daemon-reload"])
        restart_rc = _systemctl(["restart", "monitor-center"])
        if restart_rc != 0:
            print(
                "文件已按上面的模式恢复，但 systemctl restart monitor-center 没有成功（daemon-reload=%s）。请在中心上重启进程。"
                % reload_rc,
                file=sys.stderr,
            )
            return 1
        print("已执行 systemctl restart monitor-center")
    else:
        print("未重启进程（--no-systemd）。代码要等 monitor-center 重新启动才加载。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="备份或恢复已安装的中心。不升级、不部署 Agent、不做双中心。"
    )
    sub = parser.add_subparsers(dest="cmd")

    backup = sub.add_parser("backup", help="做一份与升级备份同一布局的一致副本")
    backup.add_argument("--dir", default="/opt/monitor", help="中心安装目录，默认 /opt/monitor")
    backup.add_argument("--parent", default=None, help="备份放在这个目录下，默认是安装目录的父目录")
    backup.add_argument("--unit", default=None, help="要复制的 systemd 单元；默认在文件存在时用 monitor-center.service")
    backup.add_argument("--no-unit", action="store_true", help="不复制单元文件")
    backup.add_argument("--keep", type=int, default=None, help="完成后只留最新 N 份。不传则不删除旧备份")
    backup.set_defaults(func=cmd_backup)

    restore = sub.add_parser("restore", help="从备份恢复。默认什么都不写，必须选模式并加 --yes")
    restore.add_argument("--dir", default="/opt/monitor")
    restore.add_argument("--from", dest="from_dir", required=True, help="备份目录")
    restore.add_argument("--code-only", action="store_true", help="只恢复代码、center.json、vendor 和单元，不覆盖 data/")
    restore.add_argument("--with-data", action="store_true", help="在 code-only 的基础上覆盖 data/monitor.db")
    restore.add_argument("--yes", action="store_true", help="确认改写安装目录")
    restore.add_argument("--assume-stopped", action="store_true", help="调用方已经停掉 monitor-center")
    restore.add_argument("--no-systemd", action="store_true", help="不调用 systemctl")
    restore.add_argument("--unit", default=None)
    restore.add_argument("--no-unit", action="store_true")
    restore.set_defaults(func=cmd_restore)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 2
    try:
        return int(args.func(args))
    except BackupError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
