#!/usr/bin/env python3
"""双重 fork 守护化启动，避免随父 shell / IDE 终端退出被连带杀掉。"""

import argparse
import os
import sys
import time
from typing import Optional


def become_daemon(pidfile: str, logfile: str, cwd: Optional[str]) -> None:
    """父进程直接退出；仅守护子进程继续执行后续逻辑。"""
    if os.fork() > 0:
        time.sleep(0.25)
        raise SystemExit(0)

    os.setsid()
    if os.fork() > 0:
        os._exit(0)

    if cwd:
        os.chdir(cwd)

    sys.stdin.close()
    out = open(logfile, "a", buffering=1)
    os.dup2(out.fileno(), 1)
    os.dup2(out.fileno(), 2)

    with open(pidfile, "w", encoding="utf-8") as f:
        f.write(str(os.getpid()))
        f.write("\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="守护化执行命令")
    parser.add_argument("--pidfile", required=True)
    parser.add_argument("--logfile", required=True)
    parser.add_argument("--cwd", default="")
    parser.add_argument("--env", action="append", default=[], help="KEY=VALUE，可多次")
    parser.add_argument("cmd", nargs=argparse.REMAINDER, help="-- 之后为要执行的命令")
    args = parser.parse_args()

    cmd = args.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("缺少要执行的命令", file=sys.stderr)
        raise SystemExit(2)

    for item in args.env:
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        os.environ[k] = v

    os.makedirs(os.path.dirname(os.path.abspath(args.pidfile)) or ".", exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(args.logfile)) or ".", exist_ok=True)

    become_daemon(args.pidfile, args.logfile, args.cwd or None)
    os.execvp(cmd[0], cmd)


if __name__ == "__main__":
    main()
