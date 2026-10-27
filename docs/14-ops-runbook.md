# 怎么干活：升级、便携 Python、隐藏主机

这是现行操作说明，对应仓库里的脚本。和本文冲突时以脚本为准，并应改本文。

不要另写备份、rsync 或重启来升级中心，也不要从笔记本用 `sshpass` + `scp` 装 Agent。

## 中心：首次安装和升级是两条命令

| 做什么 | 命令 | 不做什么 |
| --- | --- | --- |
| 第一次装中心 | 在中心机执行 `scripts/deploy_center.sh` | 不要用 `upgrade_center.sh` 做首次安装。安装目录不存在时它会直接退出 |
| 升级已经装好的中心 | 在工作站执行 `scripts/push_upgrade.sh` | 不要手写 `cp`、`git archive`、`rsync`，也不要 `systemctl restart` 当作换代码 |
| 装 Agent | 中心「客户端部署」页 `/deploy.html`，或在**中心机**上跑 `deploy_fleet.sh` / `deploy_agent.sh` | `scripts/install_agent.sh` 只是 `deploy_agent.sh` 的别名。笔记本侧 scp 不是第二条路径 |
| 本机试用 | `scripts/local_up.sh` / `local_down.sh` | 不要在 IDE 或 SSH 会话里只写 `python -m center &` |

`scripts/lib/sync_tree.sh` 是给 `deploy_center.sh`、`deploy_agent.sh`、`upgrade_center.sh` **source** 的函数库。直接执行它不会同步任何目录，退出码为 1，并提示回到上面的产品命令。

### 升级已安装的中心

在**有这份 git 仓库的工作站**上：

```bash
# 只检查，不改 /opt/monitor
scripts/push_upgrade.sh --check-only root@<中心> HEAD

# 正式升级。中心上跑的是这次包里的 upgrade_center.sh，不是已经装在中心上的旧脚本
scripts/push_upgrade.sh root@<中心> HEAD

# 非交互密码留在环境变量里，不放进命令行
SSHPASS='***' scripts/push_upgrade.sh root@<中心> HEAD
```

`MONITOR_PUSH_DRY=1` 只打印将使用的 SSH 命令，不连接。设置了 `SSHPASS` 但本机没有 `sshpass` 时，脚本退出，不会悄悄改用普通 `ssh`。

这条命令**只升级 `/opt/monitor`**。非 root 首次安装若落在 `~/monitor`，不能靠省略参数指到家目录。便携包同样只同步到 `/opt/monitor/vendor/python`。

工作站实际做的事：

1. 本地 `vendor/python/*.tar.gz` 存在且不是 `--check-only` 时，先在中心 `mkdir -p /opt/monitor/vendor/python`，再同步 tar.gz。`--check-only` 不把便携包写入现场目录。
2. `git archive` 该 git 版本，管道解到中心 `/tmp/monitor-upgrade-src`。这条管道**不加** `ssh -n`，否则包体传不上去。
3. 不喂数据的 ssh（跑升级脚本、建目录）使用 `ssh -n` 且标准输入接到 `/dev/null`。
4. 在中心执行：

```bash
bash /tmp/monitor-upgrade-src/scripts/upgrade_center.sh \
  /tmp/monitor-upgrade-src --sha <该版本 sha> --dir /opt/monitor
```

`--check-only` 时末尾再加 `--check-only`。

中心上的 `upgrade_center.sh` 接着做：

1. 安装目录或 `config/center.json` 不存在则退出，不生成一份空配置。
2. 从 systemd 单元读出 `LD_LIBRARY_PATH` 和 `PATH` 再选 Python。`PYTHONPATH` 指向**新包**，不是现场安装树。读单元环境必须在当前 shell，不能放进选 Python 的子 shell。
3. 在新包上跑 `python -m unittest discover -s tests`。标准输入接到 `/dev/null`。测试进程打开 `MONITOR_TEST_SANDBOX=1`，PATH 前面是只记日志的 `systemctl` / `kill` / `ssh`，临时 `HOME`，并且不再扫描 `/opt/monitor`。这样用例不会 `systemctl stop monitor-agent`。
4. 记下测试前后 `monitor-agent` 和 `monitor-center` 是否 active。测试前是 active、之后不是，就重新拉起，并在摘要里告警。
5. 测试失败则退出：**不备份、不同步、不重启**。
6. 磁盘预检：可用空间要够「约 3 倍 `data/monitor.db` 再加 20MB」。不够则退出，此时还没改安装目录。
7. `--check-only` 在测试和磁盘预检通过后打印「检查通过（未改动安装目录）」并退出。
8. 正式升级：用 SQLite `backup` API 做一致副本并 `quick_check`，再同步代码。同步保留 `config/center.json`、`data/`、`vendor/python` 里已有的 tar.gz。
9. 会重写 `monitor-center.service`，但保留原来的 `ExecStart`，并保留单元里已有的 `Environment=`（含 `PATH`、`LD_LIBRARY_PATH`）。不是为了少改单元去手写 rsync。
10. 重启后检查本机 `/api/v1/health`、`/api/v1/hosts`、`/api/v1/reports`，以及主机条数和历史上报条数没有减少。
11. 健康检查失败时只回滚代码和单元，**不用**升级前的副本覆盖 `data/`。失败的代码留在 `/opt/monitor-failed-<时间>`。
12. `--keep-backups` 默认保留 5 份。目录名 `<安装名>-backup-YYYYMMDDHHMMSS`，也认带横线的 `YYYYMMDD-HHMMSS`（可带 `-pid`）。按时间从旧到新删，不按字典序。

只改 `report_schedule` 的时刻、不换代码时，改的是中心机上的 `config/center.json`，然后重启 `monitor-center` 让进程重读配置。这不是升级。整段 `report_schedule` 可以不写，默认每天 00:10、每周一 00:20、每月 1 日 00:30（上海时间）生成上一完整周期，错过不补。

## 便携 Python

Agent 下限是 3.6。目标机没有合格解释器时，用中心上的便携 CPython，不要把约 26MB 的 tar.gz 提交进 git（`.gitignore` 已排除 `vendor/python/*.tar.gz`）。

在**有互联网**的机器上：

```bash
scripts/fetch_python_bundle.sh --dest vendor/python
scripts/push_python_bundles.sh root@<中心>
# SSHPASS='***' scripts/push_python_bundles.sh root@<中心>
```

固定包是 python-build-standalone `20241016` 的 **install_only**（不是 musl、stripped 或 `gnu_2_28`），glibc ≥ 2.17，含 x86_64 与 aarch64。中心按目标机 `uname -m` 选择。目录可在 `config/center.json` 里改：

```json
"deploy": { "python_bundles": "/opt/monitor/vendor/python" }
```

`push_python_bundles.sh` 的远端目录仍是 `/opt/monitor/vendor/python`，父目录不存在时会先 `mkdir -p`。中心升级不会删掉这里的 tar.gz。网页部署和 `deploy_fleet.sh` 在目标机没有合适解释器时上传并解压：root 解到 `/usr/local/monitor-python`，否则解到安装目录下的 `python/`。

## 隐藏主机

详情里「从列表隐藏」之后：

- 该机退出主机列表、在线/离线统计、集群时段汇总，以及**之后**新生成的诊断报告。
- 历史上报还在。已经入库的旧报告不会自动改写。
- 再上报**不会**自动取消隐藏。
- 「已隐藏」或 `GET /api/v1/hosts?hidden=1` 可以找到，详情里可恢复。
- 彻底删除要输入 `delete`（`DELETE /api/v1/hosts/{id}?confirm=delete`），连同该机历史一起去掉。没有 `confirm=delete` 则 400，不删。
- 隐藏或删除时可以同时去掉部署清单里对得上的目标。不选的话，清单还在。

同机换 IP 用详情「更换 IP」，不把隐藏当成改址。接口和字段见 `docs/03-design-and-test.md`。

## 不要做的事

- 把 `scripts/lib/sync_tree.sh` 当成升级命令，然后 `systemctl restart monitor-center`。重启不会换上新代码。
- 在笔记本上用 sshpass/scp 装 Agent，或把 Agent 装进中心的 `/opt/monitor` 覆盖中心。Agent 默认目录是 `/opt/monitor-agent`。
- 升级失败后用备份目录覆盖 `data/`。产品路径的回滚故意不动数据库。
- 往生产中心灌 `scripts/demo_seed.py`。它只适合丢掉的本地库，会写入演示主机。
