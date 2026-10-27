# 简易多机监控系统

> **这是一个超级简单的服务器资源基本监控项目。**  
> 不做 Prometheus / Grafana / Zabbix 那一套完整监控栈，只解决「多台机器的 CPU、内存、磁盘、加速卡用得咋样、是否偏高」这类基本看数需求。

不引入外部监控软件，用 **Python 3 标准库** 实现多机 **CPU / 加速卡** 资源监测、时间段统计、异常判定与客户端批量部署。

当前版本：**v1.10.0**

运行环境：

| 角色 | Python |
| --- | --- |
| **Agent（采集端）** | **3.6+**。采集代码没有 `from __future__ import annotations`，下限不是 3.7。Ubuntu 16.04 自带 3.5.2 不够；系统解释器和 apt/yum 都失败后，用中心上的便携 CPython 3.10（glibc ≥ 2.17） |
| Center（中心端） | 3.8+ |

## 定位

- **项目性质**：轻量、可本地一把梭部署的**基础资源监控**，适合实验室 / 小机房快速看一眼；不是企业级可观测平台
- **监测**：CPU（利用率 / 核数 / 负载 / 架构 / 机型）+ 加速卡（英伟达 / **AMD** / 华为昇腾 / 寒武纪 / 瑞芯微 RKNN）。厂商工具和设备节点都没有时，从 `/sys/bus/pci/devices` 识别已知加速卡（例如 `cabc:0270` → MLU270），健康状态为 `abnormal`，注明驱动缺失或设备无响应，**不**把利用率当成 0% 样本。寒武纪 `cnmon info` 只按整行 `Card N` 分卡：利用率取 `MLU Average`，温度优先 Chip、其次 Board、否则取 Cluster 最大值，显存只认 Physical Memory Usage，功耗取 Power 的 Usage，健康取 Health State 下面的 Device（`Good` 视为正常；没有这一段就留空）。各厂商先读结构化输出（`nvidia-smi` CSV、`rocm-smi` / `amd-smi` JSON；`cnmon info -j` 只在临时目录里跑，避免在工作目录留下 `cnmon_info.json`），再退回文本。工具失败的 stderr / 退出码写进卡的 note。`nvidia-smi` 版本看 `-h` 抬头，不用会在 535/470 上失败的 `--version`。同一块卡按 PCI 总线号合并
- **主机类型**：仅 `auto` | `cpu` | `gpu`（gpu=各类加速卡；兼容旧值 `app`→cpu、`npu`→gpu）
- **异常判定**：默认阈值触发后页面 **黄色(偏高) / 红色(异常)** 着色（不做告警通知通道）。CPU 负载看 load1/核数和 load5/核数，共用 `cpu_load_per_core_warn`（默认 2.0）。读不到负载时上报 null，页面显示 `-`，不写成 0
- **详情统计**：监测台分 **「实时监控」** / **「时段统计」** / **「使用诊断」** 页签。时段页可选 1h / 2h / 6h / 24h / 7d 或自定义起止，看平均 / 最低 / 最高 / P95 / 繁忙占比；支持单机与集群汇总。使用诊断在中心本地生成日/周/月报告并入库，页面预览、下载 Markdown 和主机排行 CSV（见 `docs/13-diagnostic-reports.md`）
- **主机列表**：实时监控页点击列头对 CPU% / 内存% / 磁盘% / 加速卡% / 负载 / 负责人 / 用途等排序；主机列优先显示**完整 IP**（手工展示地址、未停用的部署清单、`host_id` 内嵌 IP、RFC1918 网卡，避免短主机名片段；跳过 `198.18.0.0/15` Clash TUN / 基准测试假 IP）。负责人和用途可筛选、可搜索。历史 IP 也能搜到
- **资产信息**：每台主机可手工填写负责人、用途，以及 CPU、内存、硬盘、GPU、型号/资产编号、备注。详情里这些手工字段和 Agent 采集的规格（CPU 型号/核数、内存总量、磁盘、加速卡型号数量）并排显示。采集上报不会改写手工文字
- **隐藏与删除**：详情「从列表隐藏」后，该机退出列表、在线/离线统计、集群时段汇总和之后的诊断报告，历史上报仍在，可从「已隐藏」恢复。彻底删除要输入 `delete`（`DELETE /api/v1/hosts/{id}?confirm=delete`），连同该机历史一起去掉。可选同时删掉部署清单里 host_id 或 IP 对得上的目标。已隐藏的主机再上报也不会自动出现
- **同机更换 IP**：详情「更换 IP」先预览再确认。展示地址改为新 IP，旧 IP 记在「历史 IP」（资产 JSON 的 `previous_ips`，可带日期和备注）。监控 `host_id` 和历史上报都不改。新 IP 上已有的部署目标会把 `host_id` 改成这台主机（密码不动）；旧 IP 上属于这台主机的目标标为停用，不删除，不再驱动展示地址，也不再进入部署。按旧 IP 或新 IP 导入资产表都能对上这台主机
- **资产表导入**：实时页「导入资产」，CSV 或 xlsx，按 IP 匹配已有主机，先预览再写入。空单元格不覆盖。列名含用户名、密码、口令、password、token 的整列丢弃，不入库。示例（虚构）：`config/hosts-meta.example.csv`
- **资源组**：在实时监控页创建分组、给主机分配、按组筛选；数据在中心 SQLite，重启保留，**不必升级 Agent**
- **实时与规格分栏**：主机列表把加速卡型号数量和加速卡使用率、磁盘汇总百分比和磁盘数分成不同列。离线主机不把最后一次快照的数字当成当前利用率
- **历史保留**：`retention_days`（示例默认 7，可按环境改成更长，例如 40）。超期样本由中心清理；**已生成的诊断报告不删**。诊断报告若被保留期裁短，会在抬头标明
- **部署**：中心「客户端部署」页 `/deploy.html` 或中心机上的 `deploy_fleet.sh` / `deploy_agent.sh`（唯一产品路径）。笔记本上用 sshpass/scp 装 Agent 的做法已废弃，不要再写进操作步骤

> 批量导入只保留 **Excel/CSV**（已去掉与之重合的「文本清单」入口）。

## 本地试用（推荐）

macOS / 无 root / 在 IDE 里启动时，请用守护脚本，**不要**只用 `python3 -m center &` 或普通 `nohup`——这类进程常会随终端/IDE 会话结束一起退出，表现为「一会能开一会挂」。

```bash
chmod +x scripts/*.sh
./scripts/local_up.sh      # 启动中心 + 本机 Agent（双重 fork 守护）
# 打开 http://127.0.0.1:8080/
./scripts/local_down.sh    # 停止
```

默认目录：

| 角色 | 目录 | 日志 / PID |
| --- | --- | --- |
| 中心 | `.deploy-center/` | `run/center.log`、`run/center.pid` |
| Agent | `.deploy-agent/` | `run/agent.log`、`run/agent.pid` |

可选环境变量：`MONITOR_PORT`（默认 8080）、`MONITOR_CENTER_DIR`、`MONITOR_AGENT_DIR`、`PYTHON_BIN`（需满足上表版本；Agent 单独部署时 3.6+ 即可）。

## 唯一部署方式

**只走这一条产品路径。** 不要在笔记本上用 `sshpass` + `scp` 写临时装机脚本（那是旁路，会和中心清单/目录/失败短码脱节）。

| 角色 | 怎么装 | 默认目录 |
| --- | --- | --- |
| **Center** | 在中心机执行 `scripts/deploy_center.sh` | **`/opt/monitor`**（非 root：`~/monitor`） |
| **Agent** | 中心 UI **客户端部署** `/deploy.html`，或在**中心机上**跑 `deploy_fleet.sh` / `deploy_agent.sh` | **`/opt/monitor-agent`**（非 root：`~/monitor-agent`） |

`scripts/install_agent.sh` 只是 `deploy_agent.sh` 的兼容别名，不是第二条路径。

同机既有 Center 又有 Agent 时必须分开目录：Center 保持 `/opt/monitor`，Agent 用 `/opt/monitor-agent`，避免互相覆盖。已有清单/数据库里若仍是 `/opt/monitor`，**不会自动改写**；新产品默认与 Excel 空列才用 `/opt/monitor-agent`。

中心安装树（`/opt/monitor`）**必须保留 `agent/` 包**：网页 SSH / `deploy_fleet.sh` 从该目录打 **Agent 包**（`agent/`、`common/`、`scripts/` 与配置模板），**不会**把 `center/` 当主内容整树拷到 `/opt/monitor-agent`。源树缺 `agent/` 禁止打包；安装后、`--once` 前校验 `agent/__init__.py`，缺包短码 `package_incomplete`。

**覆盖安装清理**：先停 `monitor-agent` 与已知目录里的旧进程（`/opt/monitor-agent`、`~/monitor-agent`、`~/monitor` 的 pidfile，以及 `python -m agent` 残留），再把采集端同步进安装目录（rsync `--delete` 或等价删除后拷贝，包先解到 `/tmp/monitor-agent-src`，避免 inplace 留旧文件）。随后删除 AppleDouble `._*`、`.DS_Store`、`__pycache__`，以及安装目录里多出来的顶层项（如误拷的 `center/`）。**不删除** `config/agent.json` 与 `data/`；本轮仍按中心下发参数写入 `agent.json`。家目录旧副本只停进程，不整目录删掉。

**SSH 端口**：清单、Excel 的 `ssh_port`、网页「SSH 端口」、`deploy_fleet.sh --ssh-port` / `--port`，缺省 **22**。**不扫描端口。**

**密码登录**：必须在 **中心机**（跑 Center / `deploy_fleet.sh` 的那台）安装 `sshpass`，否则失败短码 `sshpass_missing`。没有 sshpass 就改用 SSH 密钥。不要指望笔记本侧的 sshpass 旁路。非 root 往 `/opt/monitor-agent` 装时，产品路径会先 `sudo -n`，不行再用**同一 SSH 密码** `sudo -S`，以保住已有 systemd 单元。

失败短码见下文「部署失败短码」。

### 1. 服务端（中心机）

```bash
chmod +x scripts/*.sh
./scripts/deploy_center.sh --port 8080
# 可选: --token 'your-secret' --dir /opt/monitor --no-systemd
```

成功后打开：`http://<中心IP>:8080/`  
客户端部署页（**推荐装 Agent 的方式**）：`http://<中心IP>:8080/deploy.html`  
管理设置页（管理 Token、诊断报告推送）：`http://<中心IP>:8080/settings.html`

已有中心升级时**不要删库**。`metrics_history` 可能有数 GB。诊断调度、主机列和同机改址都是加法：`diagnostic_reports` 补列，`hosts` 增加可空列 `hidden`、`hidden_at`、`asset_meta`，`deploy_targets` 增加可空列 `retired`、`retired_at`。手工展示地址和历史 IP 写在已有 `asset_meta` JSON 里。不改 `hosts.host_id`，不重写历史上报。

已安装中心的升级只走下文「升级已安装的中心」：工作站执行 `scripts/push_upgrade.sh`，中心上跑的是这次包里的 `scripts/upgrade_center.sh`。不要把 `scripts/lib/sync_tree.sh` 当成升级命令（它是函数库，直接执行不会同步代码，并以退出码 1 提示回到产品路径），也不要手写 `rsync` 再 `systemctl restart`。`upgrade_center.sh` 会重写单元文件，但保留原来的 `ExecStart` 和已有 `Environment=`（含 `PATH`、`LD_LIBRARY_PATH`）。步骤与限制见 `docs/14-ops-runbook.md`。`report_schedule` 可以不写进 `center.json`，默认每天 00:10、每周一 00:20、每月 1 日 00:30（上海时间）生成上一完整周期，错过不补。

`deploy_center.sh` 是首次安装入口。它会同步代码、保留已有 `config/center.json` 和 `data/`，并在重写单元时保留除 `PYTHONPATH`（以及本次要重写的 `LD_LIBRARY_PATH`）以外的 `Environment=` 行。它没有同步前测试，也没有「失败只回滚代码、不动 `data/`」的那套回滚。日常换代码不要改走这条脚本。

安装或升级完成、中心进程起来之后，在监测台「导入资产」先预览再写入。下线机器在详情里隐藏。已经入库的诊断报告不会自动改写；要在排行和闲忙表里看到负责人、用途，需要再生成一次。Agent 安装方式不变。

非 root 时 Center 默认装到 `~/monitor`，并以守护进程后台运行（不依赖当前终端）。

### 网页自动部署（推荐）

1. 打开 **客户端部署** 页  
2. 「中心对外访问地址」**默认自动填本机局域网 IP**（如 `http://192.168.x.x:8080`），供远端 Agent 上报；**一般不要用 127.0.0.1**  
3. **SSH**：支持用户名密码或私钥。填法：用户 + 密码；或用户 + 私钥。端口默认 **22**（清单/Excel 的 `ssh_port` 或网页「SSH 端口」；**不会扫描端口**）  
   - 全局「私钥路径」= 多台客户端**共用**一把钥匙（不是按 IP 一对一）  
   - 某台要用不同密码/私钥：在添加该客户端（或 Excel 的 `ssh_password` / `ssh_key_path` 列）单独填  
   - 密码方式：中心机需已安装 `sshpass`；否则短码 `sshpass_missing`  
   - 失败时清单「最近消息」带短码，例如 `ssh_unreachable` / `auth_fail` / `sshpass_missing`（见下表）  
4. 中心机需能 SSH 到目标机（密码或密钥任一通即可）  
5. 添加 / 编辑 / 导入目标机时，会对已配置的 IP、指定机器、中心对外地址 **自动探查**；清单「网络」列展示结果  
6. 部署前可用「测试连通/部署条件」（SSH + 安装目录可写）；再点「部署勾选 / 全部部署」  

Agent 安装目录：

- 新产品默认 **`/opt/monitor-agent`**（网页设置 / Excel 空列 / `deploy_agent.sh` 未传 `--dir`）
- 旧环境或显式填写的 **`/opt/monitor`** 仍然有效，已入库记录不迁移
- 非 root 写 `/opt/...`：中心 SSH 部署先 `sudo -n`，否则同一密码 `sudo -S`（短码 `sudo_required`）。本机直接跑 `deploy_agent.sh` 且无 sudo 时仍会落到 `~/monitor-agent`
- SSH 在线部署使用 `REMOTE_DIR`（本机脚本内部变量是 `INSTALL_DIR`）

部署页上的「目标机补跑脚本」只用于产品路径已经把代码同步到目标机之后，在该机再跑一次 `deploy_agent.sh`；**不是**从笔记本拷文件的第二条安装方式。

### 2. 中心机 CLI（同一条路径）

在**中心机**（或已能 SSH 到目标、且代码树完整的机器）执行。目标机上的 `deploy_agent.sh` 由产品路径调用，一般不必手搓 scp。

单台（已在目标机上、或 Center 同步之后）：

```bash
./scripts/deploy_agent.sh --center-url http://<中心IP>:8080
# 可选: --host-id gpu01 --token 'your-secret' --interval 15 --dir /opt/monitor-agent
```

脚本会：解析 Python（先复用本机 `PYTHON_BIN` / `python3.x` / `/usr/local/python3.*`，均需 ≥3.6；都没有且为 root 时再 apt/yum/dnf 安装 python3；安装失败不中断，继续尝试便携包）→ 同步文件（**优先 rsync**，没有则 **tar 管道** 或 **cp -a**）→ 写配置 → 探测加速卡工具 → 单次上报自检 → systemd 或守护进程常驻。systemd 和 `run/start_agent.sh` 使用最终选定的解释器。

内网常见情况：只有 3.6、PATH 里的 `python3` 过旧但 `/usr/local/python3.12` 可用、或完全没有 python3——脚本按「先复用、再安装、再便携包」处理。自定义前缀若缺 libpython，会自动加上 `LD_LIBRARY_PATH`。apt 在 Ubuntu 16.04 上若被 `appstreamcli` 打断，同样进入便携包，而不是停在 `python_install_fail`。

### 离线放置便携 Python

在**有互联网**的机器上下载（不要把约 26MB 的 tar.gz 提交进 git）：

```bash
scripts/fetch_python_bundle.sh --dest vendor/python
# 远端目录不存在时也会先 mkdir -p：
scripts/push_python_bundles.sh root@<中心>
# 非交互密码：SSHPASS='***' scripts/push_python_bundles.sh root@<中心>
```

固定包是 python-build-standalone `20241016` 的 **install_only**（不是 musl / stripped / `gnu_2_28`），glibc ≥ 2.17，含 x86_64 与 aarch64。中心按目标机 `uname -m` 选择。目录也可在 `config/center.json` 里改：

```json
"deploy": { "python_bundles": "/opt/monitor/vendor/python" }
```

中心升级不会删掉这个目录里的 tar.gz。目标机没有合适解释器时，网页部署和 `deploy_fleet.sh` 都会把包上传到远端再解压：root 解到 `/usr/local/monitor-python`，否则解到安装目录下的 `python/`。

### 升级已安装的中心

不要在中心上手写 `cp`、`git archive` 或 `systemctl restart`。在工作站执行（中心上跑的是**这次包里**的脚本）：

```bash
scripts/push_upgrade.sh root@<中心> HEAD
# 非交互密码：SSHPASS='***' scripts/push_upgrade.sh root@<中心> HEAD
# 合并前先检查，不改现场安装：
scripts/push_upgrade.sh --check-only root@<中心> HEAD
```

它等价于把该 git 版本解到中心 `/tmp/monitor-upgrade-src`，然后执行：

```bash
bash /tmp/monitor-upgrade-src/scripts/upgrade_center.sh /tmp/monitor-upgrade-src --sha <该版本 sha> --dir /opt/monitor
# --check-only 时上面那条命令末尾再加 --check-only
```

这条命令只升级 `/opt/monitor`。非 root 首次安装落到 `~/monitor` 时，不要用它去升级家目录里的那份。细节见 `docs/14-ops-runbook.md`。

脚本先从 systemd 单元读出 `LD_LIBRARY_PATH` 和 `PATH` 再选 Python（`PYTHONPATH` 指向新包，不是现场安装树），然后在**新包**上跑测试。预同步测试的标准输入接到 `/dev/null`；不喂数据的 `ssh` 使用 `ssh -n` 且 `</dev/null`，避免现场 SSH 会话的 stdin 一直不关时测试挂住。测试进程打开 `MONITOR_TEST_SANDBOX=1`，PATH 前面是只记日志的 `systemctl` / `kill` / `ssh`，临时 `HOME`，并且不再扫描 `/opt/monitor`。这样用例不会 `systemctl stop monitor-agent`。测试前后记下 `monitor-agent` 和 `monitor-center` 是否 active；若测试前是 active、之后不是，就重新拉起并在摘要里告警。失败则直接退出，不备份、不同步、不重启。`--check-only` 在测试和磁盘预检通过后打印「检查通过（未改动安装目录）」并退出，不把便携包写入现场目录，也不备份、不同步、不重启。正式升级通过后做一致的 SQLite 备份（`backup` API + `quick_check`），用现场 Storage 按 `retention_days` 清一次过期历史上报并记下条数，再同步代码（保留 `config/center.json`、`data/`、便携包）、保留 systemd 里已有的 `Environment=`（含 `PATH`、`LD_LIBRARY_PATH`）和原来的 `ExecStart`、重启，再检查 `/api/v1/health`、`/api/v1/hosts`、`/api/v1/reports`。主机变少、数据库不见或读不出来、历史上报被清空会回滚；按保留期清掉的过期历史上报不会。健康检查失败时只回滚代码和单元，**不**用升级前的副本覆盖 `data/`，失败的代码留在 `/opt/monitor-failed-<时间>`。`--keep-backups` 默认保留 5 份，目录形如 `/opt/monitor-backup-<时间>`。本地 `vendor/python` 里已有 tar.gz 时，正式推送前会先在中心 `mkdir -p /opt/monitor/vendor/python` 再同步。

步骤、备份目录里有什么、以及代码回滚和数据库恢复的差别，以 [`docs/14-ops-runbook.md`](docs/14-ops-runbook.md) 为准。

批量：

```bash
cp config/hosts.example.txt config/hosts.txt
# 编辑 IP / host_id / 可选 ssh_port（默认 22）
./scripts/deploy_fleet.sh --hosts-file config/hosts.txt --center-url http://<中心IP>:8080
# 非 22 端口：--ssh-port 2222  或  --port 2222；也可写在清单第三列 / IP:port
# Agent 目录：--remote-dir /opt/monitor-agent
```

清单格式：`IP[:port] [host_id] [ssh_port]`。行内端口覆盖 `--ssh-port`。SSH 连接超时 / connection closed 会自动重试 2～3 次（短退避），不会无限重试。

### 备份与恢复（弱高可用，不是第二条升级）

不升级时，在中心机执行 `scripts/backup_center.sh`。目录名与升级备份相同（`/opt/monitor-backup-<时间>`），里面有 `config/center.json`、一致的 `data/monitor.db`、`vendor/` 和单元文件。`scripts/restore_center.sh --code-only` 只恢复代码、配置、vendor 和单元，不动 `data/`；`--with-data` 才会覆盖数据库。双中心 / 双活 / 自动切换不做。

### 容量摸底

`scripts/capacity_probe.py` 在临时库上估算主机数、采样间隔和 `retention_days` 对应的行数与体积。冒烟不能当成容量结论，结果表要实测后才填。见 [`docs/15-capacity-lab.md`](docs/15-capacity-lab.md)。本文不写主机上限。

### 部署失败短码

成功仍只显示「部署成功」。失败为 `[短码] 说明`（网页清单与脚本日志相同）：

| 短码 | 含义 |
| --- | --- |
| `ssh_unreachable` | 连不上（超时、拒绝、无路由、DNS、连接被关闭）；先核对 IP 与 **ssh_port（默认 22）** |
| `auth_fail` | 用户名 / 密码 / 私钥认证失败 |
| `sshpass_missing` | 密码部署但**中心机**未安装 `sshpass`（请安装或改用密钥，不要从笔记本旁路） |
| `sudo_required` | 非 root 写入 `/opt/...` 时 sudo 不可用（先 `sudo -n`，否则同一 SSH 密码 `sudo -S`） |
| `key_missing` | 填写的私钥文件在中心机上不存在 |
| `no_python` | 目标机没有可用 Python ≥ 3.6，且没有传入便携包 |
| `python_install_fail` | 自动安装 python3 失败，且便携包也不可用（CentOS 7 默认 yum 镜像 EOL/404；会再试 `vault.centos.org/7.9.2009`） |
| `python_bundle_missing` | 中心没有该架构的便携包，或远端没收到文件 |
| `python_bundle_checksum` | sha256 与清单不一致 |
| `python_bundle_arch` | `uname -m` 不在 x86_64 / aarch64 |
| `python_bundle_extract` | 解压失败，或包里没有 `bin/python3` |
| `python_bundle_exec` | 解压后的解释器无法启动或仍低于 3.6 |
| `sync_tool_missing` | 同步文件失败（rsync 优先，缺失则 tar / cp 仍都不可用） |
| `package_incomplete` | 中心树或缺包导致目标没有 `agent/`（产品打的是采集端包，不是整棵 center 树；`--once` 前会校验 `agent/__init__.py`） |
| `dir_not_writable` | SSH 已通但安装目录不可写（非 root 用 `~/monitor-agent`） |
| `agent_start_fail` | 上报自检或 Agent 启动失败（中心地址 / Token / 网络） |
| `center_url_missing` | 未配置「中心对外访问地址」 |
| `timeout` | 探测或整段部署超时 |
| `remote_fail` | 远端安装失败（未归入上面几类） |
| `unknown` | 输出对不上上面任一短码 |

### 3. 卸载

```bash
./scripts/uninstall.sh --role agent                  # 默认 /opt/monitor-agent 或 ~/monitor-agent
./scripts/uninstall.sh --role center --purge-data    # 默认 /opt/monitor 或 ~/monitor
# 旧 Agent 若装在 /opt/monitor：加 --dir /opt/monitor
```

## 前台调试启动

仅适合临时看日志；关掉终端即停：

```bash
./scripts/start_center.sh                 # 前台跑中心
./scripts/start_agent.sh                  # 前台跑 Agent
./scripts/start_agent.sh --once           # 只上报一次
# PYTHONPATH=. python3 scripts/demo_seed.py   # 可选：写入演示异常场景（试用请勿执行）
```

## 启动常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 页面能开一下又连不上、进程「自己没了」 | 用 `./scripts/local_up.sh` 或 `deploy_*.sh`；勿在 Cursor/SSH 会话里只写 `python … &` |
| `Address already in use` / 健康检查失败 | 端口占用：`lsof -iTCP:8080 -sTCP:LISTEN`，必要时 `kill -9 <pid>` 后再 `local_up.sh` |
| 监测台一直空、无主机 | 中心已起但 Agent 未起或未上报；看 `.deploy-agent/run/agent.log`，确认 `center_url` 指向 `http://127.0.0.1:8080/api/v1/metrics` |
| 主机一会在线一会离线 | Agent 进程在重启或崩溃；看 agent 日志。离线判定默认约 90 秒无上报 |
| 部署页无法 SSH 安装 | 只走中心「客户端部署」或中心机 `deploy_fleet.sh`；检查用户名/密码/私钥；密码需中心机 `sshpass`（短码 `sshpass_missing`）；端口默认 22（设 `ssh_port`，不扫端口）；非 root 用 `~/monitor-agent` |
| 笔记本 scp/sshpass 装不上或覆盖了中心 | **禁止旁路**。Agent 默认 `/opt/monitor-agent`，Center 才是 `/opt/monitor` |
| `INSTALL_DIR: unbound variable` | 已修复：在线部署只用 `REMOTE_DIR`；请更新中心代码后重试 |
| 清演示数据重来 | `./scripts/local_down.sh` 后删除 `.deploy-center/data/monitor.db*`（及 wal/shm），再 `local_up.sh`；不要跑 `demo_seed.py` |
| 配置报错退出 | 看终端 `[配置错误]`；从 `config/*.example.json` 复制为 `center.json` / `agent.json` |
| 部署报「未找到可用的 Python >= 3.6」 | 设置 `PYTHON_BIN`，或安装 `python3`，或按上面的步骤把便携包放到中心。Ubuntu 16.04 的 3.5 低于 Agent 下限 |
| 部署报 `python_install_fail` / yum 404 | CentOS 7 官方镜像已 EOL。产品路径会改用 `vault.centos.org/7.9.2009`（os+updates+extras）装 python3；仍失败则使用便携包，或预装 python3≥3.6 |
| 部署报 `python_bundle_*` | 看短码：缺文件、校验、架构、解压或无法启动。apt 失败本身不会挡住便携包 |
| `error while loading shared libraries: libpython` | 已处理：使用 `/usr/local/python3.x` 时脚本会设置 `LD_LIBRARY_PATH`；请更新脚本后重装 Agent |
| macOS 无加速卡工具 | 正常，卡数为 0；仍会上报 CPU/内存等 |

自检命令：

```bash
curl -s http://127.0.0.1:8080/api/v1/health
curl -s http://127.0.0.1:8080/api/v1/hosts
tail -n 50 .deploy-center/run/center.log
tail -n 50 .deploy-agent/run/agent.log
```

## 主要 API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/metrics` | Agent 上报 |
| GET | `/api/v1/hosts` | 主机列表（含 anomaly、完整 `address`、资源组、`owner` / `purpose` / `asset`）。已隐藏的不在内。`?hidden=1` 只看已隐藏，`?include_hidden=1` 含全部 |
| GET | `/api/v1/hosts/{id}` | 详情（含手工 `asset` 与 Agent `hardware`；已隐藏的仍可按 id 打开） |
| POST | `/api/v1/hosts/{id}/meta` | 更新手工资产字段。空字符串清空该字段。`previous_ips: []` 清空历史 IP；不传该键则保留。拒绝密码一类键 |
| POST | `/api/v1/hosts/{id}/hide` | 隐藏。正文可带 `{"remove_deploy_target": true}` 同时删匹配的部署目标 |
| POST | `/api/v1/hosts/{id}/restore` | 取消隐藏 |
| DELETE | `/api/v1/hosts/{id}?confirm=delete` | 硬删除主机和历史上报。没有 `confirm=delete` 则 400，不删。`remove_deploy_target=1` 可选 |
| POST | `/api/v1/hosts/meta/import` | 按 IP 预览 CSV/xlsx。`?apply=1` 才写入。凭据列忽略。手工地址、历史 IP、展示地址、host_id 内嵌 IP 都能对上 |
| POST | `/api/v1/hosts/{id}/readdress` | 同机更换展示 IP。默认只预览。`{"new_ip","note","apply":true}` 才写入。不改 host_id，不改历史上报。apply 时记下可撤销的目标 host_id 与 retired |
| POST | `/api/v1/hosts/{id}/readdress/undo` | 撤销上一次改址。默认预览。`{"apply":true}` 恢复当时的 host_id 与 retired，清掉手工地址和这次追加的那条 `previous_ips`。没有记录则 400 |
| POST | `/api/v1/deploy/targets/{id}` | 更新部署目标。可改 `ip`、`host_id`，以及 `retired: true/false`。只提交 `retired` 时不改 IP、host_id 和密码。与其他目标 IP 冲突或 IP/host_id/`retired` 无效则 400 |
| GET/POST/DELETE | `/api/v1/groups` | 资源组列表 / 创建；`POST/DELETE /api/v1/groups/{id}` 重命名、删除 |
| POST | `/api/v1/hosts/{id}/group` | 分配资源组（`{"group_id": 1}` 或 `null` 取消） |
| GET | `/api/v1/hosts/{id}/history` | 历史趋势（`minutes` / `limit`） |
| GET | `/api/v1/hosts/{id}/period-stats` | 单机时段统计（`minutes` 或 `from_ts`/`to_ts`；avg/min/max/p95/busy_ratio） |
| GET | `/api/v1/period-stats` | 集群时段利用（每主机 + 样本加权汇总） |
| GET | `/api/v1/stats` | 集群即时统计 |
| GET | `/api/v1/anomaly` | 异常判定汇总 |
| GET | `/api/v1/export/hosts.csv` | 导出当前主机快照 |
| GET | `/api/v1/export/period-stats.csv` | 导出当前时间窗时段报表 |
| GET | `/api/v1/reports/preview?period=day\|week\|month` | 诊断报告时间窗预览（不计算） |
| POST | `/api/v1/reports/generate` | 生成日/周/月诊断报告；同一窗口覆盖已有一行。正文可带 `window_date` 或 `as_of` |
| GET | `/api/v1/reports` | 已保存的诊断报告列表 |
| GET | `/api/v1/reports/{id}` | 报告 JSON（含 Markdown 与分节） |
| GET | `/api/v1/reports/{id}/markdown` | 下载 Markdown |
| GET | `/api/v1/reports/{id}/rankings.csv` | 下载主机排行 CSV |
| GET/POST | `/api/v1/settings` | 管理 Token 与报告推送。GET 不回显明文；POST 在已启用管理 Token 时要带请求头 |
| * | `/api/v1/deploy/*` | 部署清单、设置、SSH 任务等（见部署页）。写操作在启用管理 Token 后要带请求头 |

`token`（`config/center.json` 或部署参数）只在 `POST /api/v1/metrics` 校验 Agent 上报，不是网页登录。管理 Token 是另一把可选口令，见下面「管理 Token 与报告推送」。

诊断报告的读接口直接打开。中心进程按 `report_schedule` 自动生成上一完整周期（默认日报 00:10、周报周一 00:20、月报每月 1 日 00:30，上海时间），错过不补。口径、去重、`generated_at` 与越界样本见 `docs/13-diagnostic-reports.md`（基本不用为 CPU/加速卡平均与繁忙均 <5%，不再使用 15%/10%）。

## 管理 Token 与报告推送

打开 `/settings.html`。

**管理 Token（可选）**

- 还没设置时，部署、隐藏/编辑主机、生成报告、改设置这些写操作和现在一样不校验。请在中心端口对外之前先在设置页保存，或在**第一次启动前**设置环境变量 `MONITOR_MANAGEMENT_TOKEN`（至少 8 位、不含空格）。环境变量只在库里还没有管理 Token 时写入哈希，不会覆盖已有值，也不会写进 `center.json`。
- 保存后只存 SHA-256。页面和 `GET /api/v1/settings` 都不再给出明文。
- 启用后，写接口要带请求头 `X-Management-Token: <明文>`。监测台顶栏黄条只记在当前标签页。读主机、读报告、导出不需要。
- 清空也要带当前 Token。忘记时在中心机执行：`sqlite3 <数据目录>/monitor.db "DELETE FROM admin_settings WHERE key='management_token_sha256';"`，然后立刻重新设置。
- 这把 Token **不能**代替 Agent 上报 Token，反过来也不行。

**报告推送（可选）**

- 渠道：企业微信、钉钉、飞书，填机器人 Webhook。未启用或没填地址时，`deliver_report` 什么都不发。
- 定时生成和页面「生成并保存」成功后都会推送。失败不影响报告入库。
- 钉钉请用自定义关键词「诊断」。当前不算加签。
- 接口只显示打码后的地址。完整 Webhook 留在中心数据库，便于下次发送。

细节见 `docs/13-diagnostic-reports.md`。

异常阈值见中心配置中的 `anomaly` 段（示例见 `config/center.example.json`）。

## 文档

| 文档 | 说明 |
| --- | --- |
| `docs/01-requirements.md` | 需求规格（文首是现行摘要；编号正文是起草基线） |
| `docs/02-architecture.md` | 架构设计 |
| `docs/03-design-and-test.md` | 设计与测试 |
| `docs/04-engineering-process.md` | 工程过程与已落地范围 |
| `docs/05-competitive-analysis.md` | 对标分析（2026-07 的 v1 / v1.1 快照，不是现行功能表） |
| `docs/06-optimization-backlog.md` | 历史优化 backlog |
| `docs/08-cpu-npu-focus.md` | 加速卡与部署说明 |
| `docs/11-period-utilization.md` | 时段利用统计（绝对时间窗 / 集群汇总 / P95） |
| `docs/12-disk-mounts.md` | 多挂载点磁盘：过滤规则与汇总口径 |
| `docs/13-diagnostic-reports.md` | 日/周/月使用诊断报告（本地计算、入库、预览与下载） |
| **`docs/14-ops-runbook.md`** | **怎么干活：`push_upgrade.sh`、便携 Python、隐藏主机，以及备份 / 恢复 / 回滚（弱高可用；双中心不做）** |
| `docs/15-capacity-lab.md` | 单进程 SQLite 容量探针、旋钮和空白结果表（不是 SLA） |
| **`docs/10-roadmap.md`** | **下一步：体验 U1–U3、长期运维 O1–O7，以及实装 / 容量 / 框架化 / 拓扑** |
| `docs/audit-tech-debt-2026-10-27.md` | 技术债审计。待人拍板的项在文内 NEEDS_HUMAN，不在这里实现 |

`docs/07-morning-report.md` 与 `docs/09-two-round-opt.md` 是 2026-07 的历史记录，不要当成现行操作步骤。

## 下一步（摘要）

详见 [`docs/10-roadmap.md`](docs/10-roadmap.md)。v1.10.0 先把常见任务做顺，不新增监测能力：

- **U1 总览与可读状态**（已交付顶栏、今日关注四类、白话健康、报告深链；长期空闲名单仍待）
- **U2 运维闭环**（下一优先）：部署收进同一套壳；中心升级只引导现有 `push_upgrade.sh`
- **U3 可选**：保存筛选仍待做。管理 Token 和诊断报告推送已在 `/settings.html`
- **仍保留**：N1 服务器实装、N4 容量（探针和空白结果表已有，数字待实测）、N2 框架化、N3 拓扑；长期运维见路线图 O1–O7
- **高可用**：备份 / 恢复按 `docs/14-ops-runbook.md`。双中心 / 双活继续不做

## 自测

与中心升级前同一条命令，跑 `tests/test_*.py` 全部模块（含改址、隐藏、升级、寒武纪、PCI、便携包）。推送和 pull request 上的 GitHub Actions 跑同一条命令，不安装监控栈，也不做真实 SSH。

```bash
PYTHONPATH=. python3 -m unittest discover -s tests
```
