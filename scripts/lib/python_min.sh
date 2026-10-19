#!/usr/bin/env bash
# 版本下限只写在这里。
#
# Agent 实际跑在目标机上的代码是 agent/、common/、scripts/daemonize_run.py。
# 这些文件保持 3.6 语法（没有 from __future__ import annotations、海象、PEP 585）。
# Ubuntu 16.04 自带的 3.5.2 低于这个下限，不能靠把下限改成 3.7 来“对齐”猜测。
# 中心端使用注解，下限是 3.8。
#
# shellcheck shell=bash

MONITOR_AGENT_PY_MIN_MAJOR=3
MONITOR_AGENT_PY_MIN_MINOR=6
MONITOR_CENTER_PY_MIN_MAJOR=3
MONITOR_CENTER_PY_MIN_MINOR=8
