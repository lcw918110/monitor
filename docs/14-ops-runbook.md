# 怎么干活：升级、备份与恢复、便携 Python、隐藏主机

这是现行操作说明，对应仓库里的脚本。和本文冲突时以脚本为准，并应改本文。

## 生产中心目录（NH-01，已拍板）

监控中心是**全局一套**，不分用户。生产安装目录固定 **`/opt/monitor`**。`scripts/push_upgrade.sh` 与 `scripts/push_python_bundles.sh` **不加** `--dir`，只作用于 `/opt/monitor`（便携包为 `/opt/monitor/vendor/python`）。

家目录 `~/monitor` 不走产品升级路径。本机试用使用 `scripts/local_up.sh`（默认目录是仓库内 `.deploy-center`，可用 `MONITOR_CENTER_DIR` 改），不要对试用目录执行 `push_upgrade.sh`。`deploy_center.sh` 在非 root 时仍会把默认目录改成 `~/monitor`；那只是本机落点。生产中心用 root 安装到 `/opt/monitor`。对 `~/monitor` 跑升级脚本会去找 `/opt/monitor`：目录不存在就退出；若 `/opt/monitor` 是另一套安装，则会升级那一套。

中心换代码只走 `scripts/push_upgrade.sh` → 新包里的 `scripts/upgrade_center.sh`。Agent 只走中心「客户端部署」或中心机上的 `deploy_fleet.sh` / `deploy_agent.sh`。不要另写 rsync、手写备份或 `systemctl restart` 来升级，也不要从笔记本用 `sshpass` + `scp` 装 Agent。`scripts/backup_center.sh` 和 `scripts/restore_center.sh` 不是第二条升级路径，也不是第二条 Agent 部署路径。

**双中心 / 双活 / SQLite 多写 / 自动故障切换：不做。** 这里的「弱高可用」只是：升级前和单独备份都能留下一份可恢复的副本，由人决定恢复代码还是连数据库一起恢复。

容量数字不写在这里。怎么测、结果表留空，见 `docs/15-capacity-lab.md`。

## 中心：安装、升级、备份不要混用

| 做什么 | 命令 | 不做什么 |
| --- | --- | --- |
| 第一次装生产中心 | 在中心机以 root 执行 `scripts/deploy_center.sh`，目录 `/opt/monitor` | 不要用 `upgrade_center.sh` 做首次安装。安装目录不存在时它会直接退出。非 root 落到 `~/monitor` 不是生产安装，也不走 `push_upgrade.sh` |
| 升级已经装好的中心 | 在工作站执行 `scripts/push_upgrade.sh` | 不要手写 `cp`、`git archive`、`rsync`，也不要 `systemctl restart` 当作换代码 |
| 不升级，只做一份备份 | 在中心机执行 `scripts/backup_center.sh` | 它不换代码、不重启、不部署 Agent |
| 从备份恢复 | 在中心机执行 `scripts/restore_center.sh` | 不能用它来安装新版本。新版本仍走 `push_upgrade.sh` |
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

这条命令**只升级 `/opt/monitor`**，没有 `--dir`。便携包同样只同步到 `/opt/monitor/vendor/python`，`push_python_bundles.sh` 也不加 `--dir`。`~/monitor` 与 `local_up.sh` 的试用目录都不在这条路径上，见上文「生产中心目录」。

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
8. 正式升级：用 SQLite `backup` API 做一致副本并 `quick_check`。接着用**已经装上的** `center.storage.Storage`，按 `config/center.json` 的 `retention_days`（与中心进程相同，缺省 7）对现场库清一次过期 `metrics_history`，再记下主机和历史上报条数，然后同步代码。同步保留 `config/center.json`、`data/`、`vendor/python` 里已有的 tar.gz。`--check-only` 不会做这次清理。
9. 会重写 `monitor-center.service`，但保留原来的 `ExecStart`，并保留单元里已有的 `Environment=`（含 `PATH`、`LD_LIBRARY_PATH`）。不是为了少改单元去手写 rsync。
10. 重启后检查本机 `/api/v1/health`、`/api/v1/hosts`、`/api/v1/reports`。主机变少、数据库文件不见或读不出来、历史上报被清空，都失败并回滚代码。历史上报变少但主机没少时，先等启动清理结束再读一次；若仍变少，只要现场条数不少于备份里仍在保留期内的行，就当成预期的保留期清理，不回滚。
11. 健康检查失败时只回滚代码和单元，**不用**升级前的副本覆盖 `data/`。失败的代码留在 `/opt/monitor-failed-<时间>`。
12. `--keep-backups` 默认保留 5 份。目录名 `<安装名>-backup-YYYYMMDDHHMMSS`，也认带横线的 `YYYYMMDD-HHMMSS`（可带 `-pid`）。按时间从旧到新删，不按字典序。

`scripts/backup_center.sh` 写出的目录和上面是**同一批名字**。下一次升级默认只留最新 5 份，其中也包括单独做的备份。要多留，得在中心上把 `upgrade_center.sh` 的 `--keep-backups` 调高；`push_upgrade.sh` 目前不转发这个参数，默认就是 5。

只改 `report_schedule` 的时刻、不换代码时，改的是中心机上的 `config/center.json`，然后重启 `monitor-center` 让进程重读配置。这不是升级。整段 `report_schedule` 可以不写，默认每天 00:10、每周一 00:20、每月 1 日 00:30（上海时间）生成上一完整周期，错过不补。

## 备份里有什么

升级成功走到备份那一步时，以及 `scripts/backup_center.sh`，都把副本放在安装目录**旁边**：

```text
/opt/monitor-backup-YYYYMMDDHHMMSS/
  code/                          当时的安装树
  code/config/center.json        配置，含 token、retention_days、端口
  code/data/monitor.db           sqlite3 backup API 做出的一致副本，并通过 quick_check
  code/vendor/                   便携 Python 等（现场有才有）
  monitor-center.service         单元文件副本。Environment=PATH / LD_LIBRARY_PATH 在这里
  backup-manifest.json           仅 backup_center.sh 写入。升级产生的备份可以没有这个文件
```

`data/monitor.db` 里有主机、历史上报、诊断报告、资源组和部署清单。`retention_days` 只清历史上报，不清已生成的诊断报告；整库恢复会把这些表一起带回备份当时的内容。

单独备份（不升级、不重启）：

```bash
# 在中心机上。默认目录 /opt/monitor
scripts/backup_center.sh --dir /opt/monitor

# 做完后只留最新 5 份（可选；不传 --keep 就不删旧的）
scripts/backup_center.sh --dir /opt/monitor --keep 5
```

备份使用 SQLite 的 `backup` API，中心可以继续跑。它不修改现场的 `data/monitor.db`。重复执行会再写一个新时间戳目录，不会改上一份。磁盘不够（仍是约 3 倍数据库再加 20MB）时直接退出，不留下半份目录。

没有 systemd 单元文件时，备份仍然包含代码、`center.json`、`vendor/` 和数据库，只是没有 `monitor-center.service`。

## 恢复：代码和数据库是两档

`scripts/restore_center.sh` 同时认升级备份和 `backup_center.sh` 的备份。必须写明模式，并加 `--yes`。

| 模式 | 写回 | 不动 | 何时用 |
| --- | --- | --- | --- |
| `--code-only` | 代码、`config/center.json`、`vendor/`、单元文件（备份里有，且目标可写） | **整个 `data/`**，包括 `monitor.db` | 和升级失败时的自动回滚同一边界。新代码不好用，但上报还要留着 |
| `--with-data` | 上面那些，再加 `data/monitor.db` | `data/` 里除数据库及其 `-wal`/`-shm` 以外的其他文件 | 确认要用备份当时的库，覆盖备份之后的新上报、新报告、新部署清单 |

```bash
# 只回到备份里的代码和配置，数据库保持现场
scripts/restore_center.sh \
  --from /opt/monitor-backup-YYYYMMDDHHMMSS \
  --dir /opt/monitor \
  --code-only --yes

# 连数据库一起回到备份当时。会先 systemctl stop monitor-center
scripts/restore_center.sh \
  --from /opt/monitor-backup-YYYYMMDDHHMMSS \
  --dir /opt/monitor \
  --with-data --yes
```

`--with-data` 在换库之前要求进程已经停掉。脚本会先 `systemctl stop monitor-center`。停不掉就退出，**还没改目录**。没有 systemd 时自己先停进程，再加 `--no-systemd --assume-stopped`。未停进程就换库会和 WAL 打架，所以缺 `--assume-stopped` 时直接拒绝。

同一份备份再恢复一次，结果与上一次相同：代码以备份树为准，`--with-data` 时数据库也以备份为准。

单元文件目标默认是 `/etc/systemd/system/monitor-center.service`。这个路径不可写时，脚本在改安装目录之前就退出。不需要写回单元时加 `--no-unit`。写回的是备份当时的 `Environment=`，不是现编的。

恢复之后若没有加 `--no-systemd`，脚本会 `systemctl restart monitor-center`。代码要等进程重新起来才加载。

## 回滚边界（升级自动做的 vs 人来做的）

| 场景 | 代码 / center.json / vendor / 单元 | data/monitor.db |
| --- | --- | --- |
| 升级测试或磁盘预检失败 | 现场不动 | 现场不动 |
| 升级已经同步，健康检查失败 | **自动**回到这次升级开始前的代码和单元。失败代码在 `/opt/monitor-failed-<时间>` | **不覆盖**。备份里的库留在备份目录里 |
| `restore_center.sh --code-only` | 回到你指定的那份备份 | **不覆盖** |
| `restore_center.sh --with-data` | 回到你指定的那份备份 | **覆盖**为那份备份里的一致副本。备份之后的新数据不在了 |

所以：升级脚本自己的回滚是代码回滚，不是数据库回滚。要把库也退回去，是另外一步，必须人加上 `--with-data`。

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

`push_python_bundles.sh` 的远端目录固定是 `/opt/monitor/vendor/python`，不加 `--dir`。父目录不存在时会先 `mkdir -p`。中心升级不会删掉这里的 tar.gz。网页部署和 `deploy_fleet.sh` 在目标机没有合适解释器时上传并解压：root 解到 `/usr/local/monitor-python`，否则解到安装目录下的 `python/`。

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

- 做第二个中心、SQLite 多写，或自动把流量切到备份目录。备份目录不是热备。
- 用 `restore_center.sh` 或手写 rsync 发布新代码。发布只用 `push_upgrade.sh`。
- 把 `scripts/lib/sync_tree.sh` 当成升级命令，然后 `systemctl restart monitor-center`。重启不会换上新代码。
- 在笔记本上用 sshpass/scp 装 Agent，或把 Agent 装进中心的 `/opt/monitor` 覆盖中心。Agent 默认目录是 `/opt/monitor-agent`。
- 升级失败后用备份目录覆盖 `data/`。产品路径的自动回滚故意不动数据库。要把库也退回去，必须人加上 `--with-data`，并接受丢掉备份之后的上报。
- 在容量还没按 `docs/15-capacity-lab.md` 实测之前，写「能撑多少台」。
- 往生产中心灌 `scripts/demo_seed.py`。它只适合丢掉的本地库，会写入演示主机。
