# 简易多机监控系统 — 软件需求规格说明书（SRS）

| 文档版本 | 2.0 |
| 对应产品 | v1.13.0 |
| 最近修订 | 2026-11-17 |
| 状态 | 现行规格。本文可单独阅读，与代码、`docs/13-diagnostic-reports.md`、`docs/14-ops-runbook.md` 一致 |

本文描述已经交付的产品行为。采集、存储、页面和脚本若以后变更，应同时改本文。指标框架化（路线图 N2 / NH-04，[Issue #35](https://github.com/lcw918110/monitor/issues/35)）不在本文的已交付范围，本轮也不实现。诊断报告空闲线和高负荷线在 `report` 配置里，设置页可改。

---

## 1. 引言

### 1.1 目的

本文定义「简易多机监控系统」的功能、数据、接口、非功能需求与现行边界，作为设计、实现、测试与验收的依据。

### 1.2 背景

机房或实验室里有多台 CPU 主机和带加速卡的主机。需要在一个中心里看存活、基础资源和算力占用，并按时间段、按日/周/月做固定口径的使用诊断。

不把 Prometheus、Grafana、Zabbix、Nightingale、Datadog、DCGM 等外部监控产品当作运行依赖。实现使用操作系统自带能力和 Python 3 标准库：中心接收并保存，Agent 采集回传，浏览器看页面。

### 1.3 能力范围

| 能力 | 市场常见做法 | 本系统现行范围 |
| --- | --- | --- |
| 采集模型 | Pull 或 Push | **Push**：Agent 主动 `POST /api/v1/metrics` |
| 主机指标 | node_exporter / Zabbix agent | `/proc`、sysfs 与只读命令解析：CPU、内存、负载、多挂载磁盘、网络吞吐 |
| 加速卡 | 各厂商 exporter | 英伟达、AMD、华为昇腾、寒武纪、瑞芯微 RKNN。有厂商工具就采；没有工具时，已知 PCI 设备仍上报，健康为 abnormal，利用率不当成 0% 样本 |
| 存储 | TSDB / MySQL | 中心单进程 **SQLite** |
| 展示 | Grafana | 自研中文监测台。顶栏一排：实时监控、主机、时段统计、使用诊断、运维、设置 |
| 异常 | 规则引擎 + 通知通道 | 页面按阈值黄/红着色。没有邮件、短信、电话等即时告警通道 |
| 诊断报告 | 自建报表 | 中心本地生成日/周/月报告并入库；可下载 Markdown 与排行 CSV。设置了 Webhook 时，成功生成后发到企业微信、钉钉或飞书。设置页可「测试发送」，并显示最近一次推送结果 |
| 鉴权 | LDAP / SSO / 多用户 | Agent 上报可选共享 Token。另有可选的一把管理 Token（SHA-256），不是按用户分的登录 |
| 高可用 | 多副本、自动切换 | 不做双中心、双活、SQLite 多写或自动故障切换。备份与恢复由人执行，见 `docs/14-ops-runbook.md` |

### 1.4 术语

| 术语 | 说明 |
| --- | --- |
| 中心（Center） | 全局一套服务：收指标、落库、提供 API 与页面。不分用户各装一套 |
| 采集端（Agent） | 装在被监控主机上的进程，按间隔采集并回传 |
| 主机 | 一台被监控的物理机或虚拟机，以 `host_id` 标识 |
| 主机类型 | `auto`、`cpu`、`gpu`。`gpu` 表示各类加速卡，不是只指英伟达。旧值 `app` 归一为 `cpu`，`npu` 归一为 `gpu` |
| 加速卡 | 英伟达 GPU、AMD GPU、华为昇腾、寒武纪 MLU、瑞芯微 RKNN，以及 PCI 回退认出的已知卡 |
| 指标 | 某一时刻的数值或结构化监控数据 |
| 基本不用 | 使用诊断里的空闲判定，见 3.6。不是实时页「今日关注」里的一类。只看 CPU 和加速卡平均 <5% 且繁忙 <5% |
| 重点数据机 | 在设置里被显式标进重点数据守护的主机。没标的不在范围内 |
| 数据外传异常 | 今日关注里的一类。下面四项出现任意一项，在线的重点数据机就进这一类 |
| 外传异常 | 发送速率持续不低于设置，或一段时间的发送量超过设置，或相对平时发送高出设置的倍数 |
| 路径完整性变化 | 受保护路径的文件数和大小突然一起变小，或路径下、路径旁出现大归档 |
| 未允许网段外连 | 已建立连接的目的地址不在允许网段里，也不在白名单 |
| 可移动磁盘挂载 | U 盘一类可移动磁盘被挂上 |
| 高负荷 | 使用诊断里的忙判定，见 3.6。与实时页「高负载」不是同一条规则 |
| 离线 | 实时状态。超过 `offline_seconds`（默认 90 秒）没有上报。中心看不出是机器停了还是网络断了。指标显示 `-`，保留末次快照时间 |
| 离线（无数据） | 事后历史里的空档。机器或采集端停了，或本地队列把样本丢掉了。这段没有样本，也不会再补上 |
| 断网（已补报） | 事后历史里被补上的时段。机器和采集端当时还在采样，样本先留在本机，恢复后按原来的采集时间入库 |
| 本地队列 | 采集端 `data/spool`。生产安装在 `/opt/monitor-agent/data/spool`。默认保留 24 小时 |
| 管理 Token | 设置页可选口令，保护写接口。与 Agent 上报 Token 不是同一把 |

### 1.5 约束

1. 运行时不依赖 Prometheus、Grafana、Zabbix、InfluxDB、Elasticsearch、DCGM 等外部监控软件，也不依赖第三方 pip 包。
2. Agent 使用 **Python 3.6+**（采集代码没有 `from __future__ import annotations`）。中心使用 **Python 3.8+**。目标机没有合格解释器时，使用中心上的便携 CPython（glibc ≥ 2.17，x86_64 与 aarch64），不把 tar.gz 提交进 git。
3. 加速卡采集依赖主机上已有的厂商工具或 sysfs / PCI。不随产品捆绑厂商库。
4. 监控中心是**全局一套**，不分用户。生产安装目录固定 **`/opt/monitor`**。`scripts/push_upgrade.sh` 与 `scripts/push_python_bundles.sh` **不加** `--dir`，只作用于 `/opt/monitor`（便携包为 `/opt/monitor/vendor/python`）。
5. 家目录 `~/monitor` 不走产品升级路径。本机试用使用 `scripts/local_up.sh`（默认目录是仓库内 `.deploy-center`，可用 `MONITOR_CENTER_DIR` 改）。`deploy_center.sh` 在非 root 时仍会把默认目录改成 `~/monitor`；那只是本机落点，`push_upgrade.sh` 不会升级它。生产中心用 root 安装到 `/opt/monitor`。
6. Agent 产品目录默认 `/opt/monitor-agent`，与中心目录分开。笔记本上的 sshpass + scp 不是安装路径。

---

## 2. 总体描述

### 2.1 产品行为

打开中心页面即可看到未隐藏主机的在线状态、CPU / 内存 / 磁盘 / 网络 / 加速卡、手工资产，以及时段利用和日/周/月使用诊断。顶栏是实时监控、主机、时段统计、使用诊断、运维、设置。实时页用「今日关注」指出当前该看的离线、磁盘快满、卡异常和高负载。长期空闲只在使用诊断的「基本不用」里。磁盘占用看容量，磁盘读写看 Mbps 和 %util，两者都不进入闲忙。中断期间只说「离线」；恢复后历史才区分「离线（无数据）」和「断网（已补报）」。

### 2.2 角色

全站共用这一套中心，没有按人拆分的数据或安装。

| 角色 | 能做的事 |
| --- | --- |
| 系统管理员 | 在中心机安装生产中心、用工作站升级 `/opt/monitor`、分发 Agent、可选地设置管理 Token 与报告推送 |
| 查看和维护者 | 浏览监测台；在未设置管理 Token 时，能访问中心端口的人也可以改资产、隐藏、改址、部署和生成报告。设置管理 Token 之后，这些写操作要带凭证。读监测台不要求这把 Token |

管理 Token 不是多用户账号，也不能拿来代替 Agent 上报 Token。

### 2.3 运行环境

- 操作系统：Linux 为生产环境；macOS 只用于开发验证和本机试用
- 网络：Agent 能访问中心 HTTP 端口。部署时中心机能 SSH 到目标机，SSH 端口默认 22，不扫描端口
- 密码登录部署需要**中心机**已安装 `sshpass`；否则改用私钥
- 加速卡工具可选。没有工具时系统指标仍然上报

### 2.4 部署拓扑

```
  [cpu Agent]  ──┐
  [gpu Agent]  ──┼── HTTP POST /api/v1/metrics ──▶ [Center /opt/monitor]
  [auto Agent] ──┘                                      │
                                                        ├─ SQLite（data/）
                                                        ├─ 监测台 /  部署页 /deploy.html  设置页 /settings.html
                                                        └─ 可选：诊断报告 Webhook
```

| 做什么 | 入口 | 目录 |
| --- | --- | --- |
| 生产中心首次安装 | 中心机 root 执行 `scripts/deploy_center.sh` | `/opt/monitor` |
| 升级已安装的生产中心 | 工作站 `scripts/push_upgrade.sh`（先 `--check-only`） | 只升级 `/opt/monitor`。不加 `--dir` |
| 同步便携 Python | `scripts/push_python_bundles.sh` | 只写 `/opt/monitor/vendor/python`。不加 `--dir` |
| 本机试用 | `scripts/local_up.sh` / `local_down.sh` | 默认仓库 `.deploy-center` 与 `.deploy-agent`。不走升级脚本 |
| 安装 Agent | `/deploy.html`，或中心机 `deploy_fleet.sh` / `deploy_agent.sh` | 默认 `/opt/monitor-agent` |

`scripts/lib/sync_tree.sh` 是函数库。直接执行它不会升级，退出码为 1。不要用手写 rsync 或 `systemctl restart` 代替 `push_upgrade.sh`。`deploy_center.sh` 是首次安装入口，没有同步前测试和「失败只回滚代码、不动 `data/`」的回滚。

升级保留 `config/center.json`、`data/`、已放置的便携包，以及单元里已有的 `Environment=` 和原来的 `ExecStart`。健康检查失败时只回滚代码和单元，不覆盖 `data/`。不升级时的备份、以及人执行的代码恢复或连库恢复，见 `docs/14-ops-runbook.md`。

---

## 3. 功能需求

### 3.1 中心部署、升级与保留（FR-01）

| ID | 需求 | 优先级 |
| --- | --- | --- |
| FR-01-01 | 生产中心单机即可运行。监听地址和端口可配，示例 `0.0.0.0:8080` | P0 |
| FR-01-02 | 数据目录可配，默认 SQLite `data/monitor.db` | P0 |
| FR-01-03 | 生产安装目录固定 `/opt/monitor`。升级与便携包脚本不加 `--dir` | P0 |
| FR-01-04 | 已安装中心只经 `push_upgrade.sh` → 新包里的 `upgrade_center.sh` 换代码。先 `--check-only`，测试或磁盘预检失败则不改安装目录 | P0 |
| FR-01-05 | `retention_days` 可配（示例 7，现场可设为 40）。清理只删除过期 `metrics_history`，不删除已生成的诊断报告 | P0 |
| FR-01-06 | 离线阈值 `offline_seconds` 可配，默认 90 秒无上报视为离线 | P0 |
| FR-01-07 | 本机试用走 `local_up.sh`，不进入产品升级路径 | P1 |

### 3.2 采集与回传（FR-02）

Agent 按 `interval_seconds` 采集（默认 15，下限 5）并 POST。短重试仍失败时，把这一笔按原来的 `timestamp` 写入安装目录 `data/spool`，进程不退出，下一周期再试。发送成功后，再向 `POST /api/v1/metrics/batch` 补发一小批（默认 80 条，不超过 100），样本带 `backfill: true`，时间戳仍是采集时刻。队列默认保留 24 小时（`spool_retention_hours`，设置页「断网补报保留」；成功上报后中心把小时数下发给采集端）。超期或条数到达上限时丢掉最旧样本，并记下溢出，随下一次补报告诉中心。进程重启和覆盖安装保留 `data/`，所以队列还在。机器停机或磁盘没了，这段补不回来。上报体含 `host_id`、`hostname`、解析后的 `host_type`、`timestamp`、`system`、加速卡字段和 `agent_version`。配置了上报 Token 时带 `token`。

`host_id` 默认主机名，可配置。`host_type` 为 `cpu` 时只采系统指标。`gpu` 与 `auto` 再采加速卡；`auto` 有卡则为 `gpu`，否则为 `cpu`。

系统指标：

| 项 | 规则 |
| --- | --- |
| CPU | 利用率、核数、架构、型号、频率、开机时长 |
| 负载 | `load1` / `load5` / `load15`。读不到的那一档为 null，页面显示 `-`，不当成 0。操作系统报出的 0 仍是 0 |
| 内存 | 总量、已用、百分比 |
| 磁盘容量 | 多挂载 `disks[]`（挂载、设备、文件系统、容量、已用、百分比）。`disk_total_gb` / `disk_used_gb` 为纳入挂载的容量之和；`disk_percent` 为各挂载利用率的最大值；`disk_count` 为条数。过滤虚拟盘、网络盘、容器/快照路径、小于 1 GiB 的非主路径等，规则见 `docs/12-disk-mounts.md`。`disk_path`（默认 `/`）始终计入。旧 Agent 没有 `disks[]` 时按单盘。容量和读写分开，互不换算 |
| 磁盘读写 | 整盘 `disk_read_mbps` / `disk_write_mbps`（纳入磁盘之和，Mbps，与网络同一单位：扇区 512 字节再 ×8÷10^6）和 `disk_io_util_percent`（纳入磁盘中设备忙闲百分比的最大值，类似 iostat %util，限制 0～100，没有额定磁盘带宽）。可选 `disk_io[]`（name、read_mbps、write_mbps、util_percent）。只计 sda、nvme0n1 这类整盘，不计分区，跳过 loop、ram、zram、sr 等。用上一笔 `/proc/diskstats` 做差；刚启动还没有上一笔时这三项为 null。旧历史上报没有这些字段时按缺失处理，不补 0 |
| 网络 | 非虚拟网卡汇总的入/出向 Mbps、累计字节、链路额定 Mbps，以及相对额定的入/出向百分比。没有计数时这些字段为 null |
| 地址 | 本机主要 IPv4。跳过回环、链路本地和 `198.18.0.0/15` 隧道假 IP |

加速卡统一进 `accelerators[]`，并拆出兼容字段：`gpus` 为英伟达与 AMD，`npus` 为其余卡。列表展示用 `accel_summary`（厂商 + 型号 + 数量），与「加速卡使用率」分列。

| 厂商 | 采集 |
| --- | --- |
| 英伟达 | `nvidia-smi`。有卡时另采 `gpu_processes`。版本看 `-h` 抬头 |
| AMD | 优先 `rocm-smi`，失败再 `amd-smi` |
| 华为昇腾 | `npu-smi`。利用率只接受 0～100，解析时丢掉越界值 |
| 寒武纪 | 优先临时目录里的 `cnmon info -j`，再按整行 `Card N` 解析。`Good` 视为正常，不因此报健康告警 |
| 瑞芯微 RKNN | rknpu 相关节点 |
| PCI 回退 | 没有厂商工具和设备节点时，从 `/sys/bus/pci/devices` 认出已知厂商设备。健康 `abnormal`，利用率留空，不进入 0% 样本。认不出则为空列表。不影响 CPU 等系统指标 |

同一块卡按 PCI 总线号合并。工具失败时，stderr 或退出码写入该卡 note。页面把健康说成人话（驱动缺失、工具不在 PATH、硬件异常等）；原始 `health` 仍留在数据里。

### 3.3 实时展示、资产、隐藏与改址（FR-03）

监测台只有一条顶栏：实时监控、主机、时段统计、使用诊断、运维、设置。实时监控是今日关注和主机表；主机仍打开同一页并定位到主机表；时段统计和趋势、使用诊断是另外两页。部署页仍是 `/deploy.html`，设置页是 `/settings.html`。页面约 15 秒自动刷新。离线主机不把最后一次快照的数字显示成当前利用率。读不到的数显示 `-`。时段趋势悬停显示 Asia/Shanghai 时间和该点数值；没有补上的空档数值是横线，并写「离线（无数据）」；补上的点写「断网（已补报）」。给人看的 CSV 用中文表头并另附字段说明，`hosts.json` 的键保持英文。

| ID | 需求 | 优先级 |
| --- | --- | --- |
| FR-03-01 | 主机列表：名称或地址、类型、在线、最近上报、CPU / 内存 / 磁盘 / 加速卡利用率、负载、负责人、用途、资源组。列头可排序 | P0 |
| FR-03-02 | 展示地址优先手工地址，其次未停用的部署清单，再是 `host_id` 里的完整 IP。跳过 `198.18.0.0/15`。历史 IP 可搜索 | P0 |
| FR-03-03 | 详情同时给出手工资产和 Agent 采集规格（CPU 型号与核数、内存、磁盘分盘、加速卡型号与数量）。采集上报不覆盖手工字段 | P0 |
| FR-03-04 | 集群统计：未隐藏主机的总数、在线数、加速卡数量与 `accel_summary`、在线主机的平均 CPU / 内存 / 加速卡利用率 | P0 |
| FR-03-05 | 只读 JSON API 与主机快照 CSV / JSON 导出 | P0 |
| FR-03-06 | 手工资产存在中心：负责人、用途、名称、分类、宿主机、是否资源监控、CPU、内存、硬盘、GPU、型号/资产编号、备注、展示地址、历史 IP。可按负责人筛选。CSV / xlsx 按 IP 预览后写入；空单元格不覆盖；列名含用户名、密码、口令、password、token 的整列丢弃 | P0 |
| FR-03-07 | 隐藏可恢复。隐藏后退出列表、在线/离线统计、集群时段汇总，以及**之后**生成的诊断报告。历史上报还在。再上报不会自动取消隐藏。硬删除必须 `confirm=delete`，否则 400，连同该机历史一起去掉。隐藏或删除时可选同时去掉对得上的部署目标 | P0 |
| FR-03-08 | 同机更换展示 IP：默认预览，`apply: true` 才写入。不改 `host_id`，不改历史上报。旧 IP 进入历史 IP；新 IP 上已有部署目标改挂到这台主机（密码不动）；旧 IP 上属于这台主机的目标标为停用，不删除。可撤销上一次改址。`previous_ips: []` 清空历史 IP | P0 |
| FR-03-09 | 资源组只存在中心 SQLite。可创建、重命名、删除、分配，并按组筛选。不必升级 Agent | P1 |
| FR-03-10 | 「今日关注」按当前快照统计四类，点一类就筛到主机表：离线；磁盘达到 `disk_critical_percent`（默认 ≥95%）或磁盘异常；加速卡健康异常；高负载。高负载指 CPU 达到 `cpu_critical_percent`（默认 ≥95%），或 load1/核数、load5/核数达到 `cpu_load_per_core_warn`（默认 2.0）。离线主机只进离线，不用末次快照再算磁盘或负载。长期空闲不在这里计算 | P1 |

### 3.4 在线判定与页面着色（FR-04）

超过 `offline_seconds`（默认 90）未上报为离线。这是唯一的实时状态，不另做「网络中断」徽章。离线主机的资源判定为 unknown，不把末次快照说成当前高低。历史上报按采集时间排列：补报不会把过去的点画到恢复连接的那一刻。同一 `(host_id, timestamp)` 只留第一笔。

在线主机按 `config/center.json` 的 `anomaly` 着色：偏高为黄，异常为红。未写的键用下表。旧键 `npu_*` 与 `accel_*` 并存；新键缺省时沿用旧键，不删除别名。

| 资源 | 偏高 | 异常 |
| --- | --- | --- |
| CPU 利用率 | ≥80% | ≥95% |
| CPU 负载 | load1/核数或 load5/核数 ≥ 2.0 | 无单独红线。load15 只采集和展示 |
| 内存 | ≥85% | ≥95% |
| 磁盘 | ≥85%（取 `disks[]` 与汇总里的更大值，文案带最满挂载） | ≥95% |
| 加速卡利用率 | ≥95% | 无单独红线 |
| 加速卡温度 | ≥85°C | ≥95°C |
| 加速卡显存 | ≥90% | ≥98% |
| 加速卡健康 | Warning 等非正常 | Alarm / Critical；PCI 回退的 abnormal 进入卡异常 |
| 利用率越界 | 小于 0 或大于 100、NaN 标为无效，不拿去和高低阈值比 | — |

null 负载不参与比较。这组阈值只驱动实时着色和「今日关注」，不改变 3.6 的诊断报告空闲线与高负荷线。

### 3.5 时段利用（FR-UTIL）

单机 `GET /api/v1/hosts/{id}/period-stats` 与集群 `GET /api/v1/period-stats`。相对窗口用 `minutes`（预设 60 / 120 / 360 / 1440 / 10080；省略时时段接口默认 120）。绝对窗口用成对的 `from_ts` / `to_ts`；两者都给时以绝对窗口为准。窗口裁剪到 `retention_days` 与当前时刻；整段都在保留期之外则 400，不编造利用率。

每个指标给出 avg、min、max、p95、busy_ratio、sample_count。统计 CPU、内存、磁盘占用率、磁盘读取/写入 Mbps、磁盘 I/O 利用率、load1、网络接收/发送 Mbps、加速卡平均利用率与时段内最高温度，以及相对额定的网络百分比。没有该字段的旧点不进入对应指标的样本。磁盘读写和磁盘 I/O 利用率默认没有繁忙阈值，busy_ratio 为空。集群汇总是**样本加权**（`sample_weighted`）：把未隐藏主机在窗口内的原始样本合在一起再算 avg / p95 / busy_ratio，min/max 取全局最值。已隐藏主机不进入集群时段接口；按 id 仍可看单机，包括已隐藏的。

繁忙占比默认 CPU ≥80%、加速卡 ≥80%，来自 `period_util.busy_cpu_percent` / `busy_accel_percent`，查询参数 `busy_cpu` / `busy_accel` 可覆盖。可选内存、磁盘繁忙线。负的查询值表示该指标不算繁忙占比。改这组 80% **不会**改诊断报告里的空闲线或高负荷线；那两条在 `report`，未改时默认是 5% 与 40% / 85%。

详情趋势 `GET /api/v1/hosts/{id}/history` 可带同一时间窗；不带时默认 60 分钟。时段结果可导出 CSV。

时段页先是时间窗和导出，下面左侧是主机表。表头是离线概览，分开「离线（无数据）」和「断网（已补报）」的段数、累计时长和起止。右侧是选中主机的汇总，趋势图铺在下方。悬停趋势图显示上海时间和数值。点一台主机，右侧先写这两种时段，再写利用率。判定用已有历史上报和 `offline_seconds`（一条上报覆盖其后这段时间，含端点），不新增传感器，也不改变闲忙两极。补上的点和平时一样进入平均、P95 和繁忙占比。主机第一次上报之前不算离线。点选主机只更新右侧汇总和下方趋势，不重绘整页；连点时忽略过期响应。

### 3.6 使用诊断报告（FR-DIAG）

中心在本地把窗口内的原始历史上报聚合成中文报告，写入 SQLite 表 `diagnostic_reports`。读已保存的报告不重算。不把日报、周报拼成月报。页面在 `/#reports`。调度在中心进程内，不依赖外部 cron。

时区 Asia/Shanghai（UTC+8，无夏令时）。窗口半开：含起点，不含结束时刻。

| 类型 | 默认时刻 | 窗口 |
| --- | --- | --- |
| 日报 | 每天 00:10 | 昨日 00:00～今日 00:00；对比再前一日 |
| 周报 | 每周一 00:20 | 上周一 00:00～本周一 00:00；对比再前一周 |
| 月报 | 每月 1 日 00:30 | 上月 1 日 00:00～本月 1 日 00:00；对比再上一自然月 |

`report_schedule` 可整段省略，省略时用上表，不改动已有的 `token` 与 `retention_days`。`enabled: false` 可关掉全部或单独一档。`hour` 必须是 0–23，`minute` 必须是 0–59，非法则中心拒绝启动。只在配置的那一分钟、进程还活着时触发。停机错过的分钟不补。进程在这一分钟内重启会再试一次；该窗口已有成功或失败行则跳过，不覆盖。

`as_of` 只决定「今天」落在哪一天，从而取上一个已经结束的完整周期。`window_date`（`YYYY-MM-DD`）指定已经结束的那一天、那一周或那一月。两者都给时以 `window_date` 为准。实际计算时间记在 `generated_at`，不是 `as_of`。日期不合法或窗口还没结束：400，不入库。起点早于保留期时抬头写明 clamped；整段都在保留期外：400，留下 `status=failed`，不编造利用率。

同一个 `(period_type, requested_from_ts, requested_to_ts)` 只保留一行。页面或 `POST /api/v1/reports/generate` 再次生成同一窗口则覆盖，`id` 不变，`source` 改为 `manual`。调度器见到该窗口已有任一行则跳过。请求体里的 `source` 忽略。

报告直接聚合原始样本。繁忙占比沿用 3.5 的样本加权和繁忙阈值（默认 ≥80%，`period_util` 可改）。下面两极的默认是平均 <5%、繁忙占比 <5%（`idle_busy_lt` 默认 0.05）、高负荷繁忙占比 ≥0.40 或 P95 ≥85%、最少样本 10。这些数在 `config/center.json` 的 `report` 段（`idle_avg_lt`、`idle_busy_lt`、`high_busy_gte`、`high_p95_gte`、`min_samples`），设置页可改；不写就用代码里的同一套默认。已入库的旧报告不自动重算。

闲忙两极**只看 CPU 和加速卡**。磁盘占用、磁盘读写、磁盘 I/O 利用率和内存都不进闲忙。

| 判定 | 条件 |
| --- | --- |
| 高负荷 | 该指标样本数达到 `report.min_samples`（默认 10），且繁忙占比 ≥ `high_busy_gte`（默认 0.40）或 P95 ≥ `high_p95_gte`（默认 85%）。CPU 满足即算；有加速卡且加速卡样本也够时，加速卡同样可以单独算高负荷 |
| 基本不用 | **生成时刻在线**，且 CPU 样本达到最少样本数，CPU 平均 < `idle_avg_lt`（默认 5%）**并且** CPU 繁忙占比 < `idle_busy_lt`（默认 0.05）。有加速卡时，加速卡样本也须达到最少样本数，加速卡平均和繁忙占比同样低于这两条。无加速卡则只看 CPU |
| 样本不足 | 在线但样本不够，或有卡却没有足够的加速卡样本。不判成基本不用，也不编数 |
| 离线 | 生成时刻已离线。只写末次上报时间，不把末次快照当成实时用量。这是实时状态，不是「离线（无数据）」 |

已落入高负荷的主机不再计入基本不用。更早模板用过的「CPU 平均 <15%、加速卡平均 <10%」不是现行规则。

窗口在线率 = 该窗口内至少有一条历史上报的主机数 / 当前未隐藏主机数。已隐藏主机不进入分子、分母、排行和闲忙。没有单独的在线心跳表。闲忙里的「离线」和「基本不用」看生成时刻是否在线，不回溯窗口内状态。报告另写窗口内「离线（无数据）」和「断网（已补报）」的段数与秒数。补报按采集时间计入利用率。本地队列溢出丢掉的样本记在「离线（无数据）」，并在正文点明，不是静默空档。

窗口内上报条数为 0 时，日/周/月报告仍然生成并入库（`empty=true`，`status=ok`）。列表和预览都保留报告壳，并写明没有可汇总的上报、不编造利用率。这不是失败。失败仍是窗口不合法或整段超出保留期。

排行、闲忙和排行 CSV 带手工负责人、用途。没填时正文写「未填」，CSV 为空字符串。这些字段来自中心资产，采集端不改写。已经入库的旧报告不自动补列，需要对该窗口再生成一次。隐藏发生在生成之后才对新报告生效。

磁盘单独成节：集群平均、P95、最高；主机按窗口内最高 `disk_percent` 分成 ≥85% 与 ≥95%（与 `anomaly.disk_warn_percent` / `disk_critical_percent` 一致）。多挂载的最满路径来自该窗口历史点里的 `disks[]`。历史里没有挂载列表时写「挂载样本不足」，不用生成时刻的快照回填。磁盘满不等于算力忙。

CPU、内存、磁盘、加速卡利用率只接受 0～100%（含两端）。负值、大于 100%、NaN、无穷大从平均、P95、繁忙占比里排除，不当成 0。报告写明排除条数、指标和主机。实时异常判定把越界标成无效。历史行不批量改写。

报告结构：抬头（类型、时间窗、对比窗、口径、保留期裁剪）、总览、集群资源利用、主机排行、闲忙两极、磁盘、资源组（有分组时）、诊断与建议、附录。单机读取上限 250000 行（时段统计接口默认仍是 60000）。触达上限时附录写明，不从别的报告补数据。诊断建议不根据部署日志推断「部署残留」。

定时和手动生成成功后调用 `deliver_report`。设置页启用了 Webhook 才发送；未配置则不发请求，报告仍入库。推送失败只记最近一次结果，不把这次生成改成失败。正文过长会截断。这不是今日关注、离线或数据外传异常的即时推送。

`retention_days` 不删除 `diagnostic_reports`。报告可以在原始点过期后继续打开。

### 3.7 管理 Token 与报告推送（FR-ADMIN）

配置在中心库 `admin_settings`，不进 `config/center.json`。升级保留 `data/` 时会留下来。同一设置页还编辑 `center.json` 里中心实际读取的参数（保留与清理、异常着色、诊断报告阈值、时段利用率、报告时刻、Agent 上报 Token、监听地址等），按组展示，未配置用内置默认并可恢复。除监听地址、端口、数据目录要重启外，保存后当前进程按新值工作。管理 Token 和 Webhook 仍只在库里，接口不回显明文。管理 Token 不是保存 Webhook 或启用推送的前提：没设 Token 时写接口仍然开放；中心端口对别人可见之前，仍建议先设上。

| ID | 需求 | 优先级 |
| --- | --- | --- |
| FR-ADMIN-01 | 未设置管理 Token 时，部署、改主机、生成报告、保存设置等写接口不校验。读接口始终开放 | P1 |
| FR-ADMIN-02 | 已设置时，写接口必须带 `X-Management-Token`。缺失或错误为 401，`code` 为 `management_token_required`。保存的是 SHA-256，GET 不返回明文或哈希 | P1 |
| FR-ADMIN-03 | 首次启动前可用环境变量 `MONITOR_MANAGEMENT_TOKEN`（至少 8 位、不含空格）。只在库里还没有管理 Token 时写入哈希，不覆盖已有值，不写回 `center.json` | P1 |
| FR-ADMIN-04 | Agent 的 `token` 只校验 `POST /api/v1/metrics` 和 `POST /api/v1/metrics/batch`。两把 Token 不能互换。补报不要求管理 Token | P0 |
| FR-ADMIN-05 | 报告推送渠道为企业微信（markdown）、钉钉（markdown；当前不加签，机器人关键词需包含「诊断」）、飞书（text）。只接受 http/https Webhook。读取接口只返回渠道、是否启用、打码后的地址，以及最近一次推送结果。不返回 Webhook 原文 | P1 |
| FR-ADMIN-06 | 设置页「测试发送」用已保存的渠道和 Webhook 发一条含「诊断」的短消息，不生成诊断报告。未启用或没有 Webhook 时不访问网络。最近一次结果记下成功或失败原因、北京时间和这次是测试发送还是报告推送。传输失败再试两次；推送失败不把报告生成改成失败 | P1 |

### 3.8 Agent 安装（FR-DEPLOY）

Agent 只从 `/deploy.html` 或中心机 `deploy_fleet.sh` / `deploy_agent.sh` 安装。`install_agent.sh` 是 `deploy_agent.sh` 的别名。笔记本 sshpass + scp 不是第二条路径。

中心安装树必须带 `agent/` 包。下发的是采集端包（`agent/`、`common/`、`scripts/` 与配置模板），不是整棵 `center/` 树。缺包短码 `package_incomplete`。

默认远端目录 `/opt/monitor-agent`。清单里已经写过的 `/opt/monitor` 不自动改写。非 root 写 `/opt` 时，产品路径先 `sudo -n`，再用同一 SSH 密码 `sudo -S`。密码登录但中心机没有 `sshpass` 时短码 `sshpass_missing`。

失败为 `[短码] 说明`。短码包括 `ssh_unreachable`、`auth_fail`、`sshpass_missing`、`sudo_required`、`key_missing`、`no_python`、`python_install_fail`、`python_bundle_missing`、`python_bundle_checksum`、`python_bundle_arch`、`python_bundle_extract`、`python_bundle_exec`、`sync_tool_missing`、`package_incomplete`、`dir_not_writable`、`agent_start_fail`、`center_url_missing`、`timeout`、`remote_fail`、`unknown`。

解释器顺序：复用已有 ≥3.6 的 Python，再尝试安装，再使用便携包。便携包由 `fetch_python_bundle.sh` 在有网机器下载，再由 `push_python_bundles.sh` 放到中心 `/opt/monitor/vendor/python`。

---

### 3.9 重点数据守护（FR-GUARD）

只覆盖在设置页被标成**重点数据机**的主机，可以按单台，也可以按资源组。主机上的设置优先于资源组。没标的主机不采集、不入库、不进今日关注。

目的是发现重点数据**可能正在离开**的迹象，不能保证数据出不去。拦截属于防火墙和目录权限。本功能不拦截、不丢弃流量，不挂钩所有文件读取，不读取或上传文件内容，不抓包，不记录按键。

设置项（业务说明写在设置页上）：

1. 把选中的主机或资源组标为重点数据机。
2. 受保护路径（目录或文件）。不要填根目录。
3. 可选的允许目的网段（内网）。填了之后，连到这些网段之外计为**未允许网段外连**。留空则不做网段判断。
4. 外传阈值：持续发送速率（Mbps）和/或一段时间的发送量（MB），都可以单独留空。可选再填「相对平时发送的倍数」，用已有的 `net_tx_mbps` 跟更早的样本比。不新增网卡指标。
5. 白名单：已知备份或同步的目的地址、进程名。对得上时不报外传异常、未允许网段外连；白名单进程正在运行时，也不把可移动磁盘挂载算进异常。

采集端在重点数据机上做这些事，报文里不带文件内容：

1. 路径清点间隔默认 5 分钟。记录文件个数、总字节、最近修改时间。扫不完或读失败时不把残缺结果当成变小。可选的轻量指纹只哈希路径、大小和修改时间，有个数上限，树太大就留空。突然变小（个数和字节按比例一起下降）记为路径完整性变化；受保护路径下面或紧挨着的目录里新出现的大归档也记。
2. 外传用本机已经采集的 `net_tx_mbps`，按上面的阈值判断。读不到速率时保持空，页面显示 `-`，不写成 0。
3. 可移动磁盘看挂载和块设备的 removable 标记。外连用 `ss` 或 `netstat` 的已建立连接做汇总（目的地址、端口、条数、能看到的进程名），不抓包。
4. 短时发送失败时，重点数据观察跟这一笔指标一起进入现有的 `data/spool`（`agent.spool.enqueue_data_guard`）。恢复后按原来的采集时间补报，中心再按当时的策略重算。不另做一套 24 小时指标队列。

中心在实时页给重点数据机一个徽章。有上述事件时，在线主机进入今日关注的「数据外传异常」（就这一个名字）。时段统计和使用诊断给出外传异常、路径完整性变化、未允许网段外连、可移动磁盘挂载的次数和时长。导出表头用中文并带单位，JSON 键保持英文。使用诊断若已配置报告推送，这一节随原有渠道发出，不另做通知。

`disk_percent` 仍只表示容量。闲忙仍只看 CPU 和加速卡。

## 4. 非功能需求

| ID | 类别 | 描述 |
| --- | --- | --- |
| NFR-01 | 性能 | 单进程中心在默认约 15 秒上报下持续写入。能同时带多少台以容量探针的实测表为准，本文不写主机上限 |
| NFR-02 | 可靠性 | 重启后最新快照和历史仍在。明细保留可配置天数。升级失败的自动回滚不覆盖数据库 |
| NFR-03 | 可维护性 | `center/`、`agent/`、`common/`、`docs/`、`scripts/`、`tests/` 分离。自测命令为 `PYTHONPATH=. python3 -m unittest discover -s tests` |
| NFR-04 | 安全 | 上报 Token 与管理 Token 都可选。生产放在内网。资产导入不收凭据列 |
| NFR-05 | 可移植性 | 标准库即可运行。便携解释器只在目标机没有合格 Python 时使用 |
| NFR-06 | 易用性 | 中文界面。加速卡健康和今日关注用人话。报告里的主机行可打开 `#host=` 详情 |

---

## 5. 数据需求

### 5.1 上报报文

字段可随厂商多几个键。中心认识的块如下。`gpus` / `npus` 是兼容拆分，完整列表在 `accelerators`。

```json
{
  "host_id": "gpu-01",
  "hostname": "gpu-01.lab",
  "host_type": "gpu",
  "timestamp": 1722096000,
  "token": "optional-agent-token",
  "agent_version": "1.12.0",
  "system": {
    "cpu_percent": 23.5,
    "cpu_count": 64,
    "cpu_arch": "x86_64",
    "cpu_model": "示例 CPU",
    "mem_total_mb": 128000,
    "mem_used_mb": 64000,
    "mem_percent": 50.0,
    "disk_total_gb": 1800,
    "disk_used_gb": 900,
    "disk_percent": 50.0,
    "disk_read_mbps": 8.192,
    "disk_write_mbps": 1.024,
    "disk_io_util_percent": 12.5,
    "disk_count": 2,
    "disks": [
      {"mount": "/data", "device": "/dev/sdb1", "fstype": "xfs", "total_gb": 1600, "used_gb": 800, "percent": 50.0}
    ],
    "load1": 2.1,
    "load5": 1.8,
    "load15": 1.4,
    "uptime_sec": 864000,
    "net_rx_mbps": 12.5,
    "net_tx_mbps": 3.2,
    "net_rated_mbps": 1000,
    "net_rx_percent": 1.25,
    "net_tx_percent": 0.32,
    "primary_ip": "10.0.0.8"
  },
  "accelerators": [
    {
      "index": 0,
      "vendor": "nvidia",
      "vendor_label": "英伟达",
      "name": "NVIDIA A100-SXM4-40GB",
      "util_percent": 80,
      "mem_total_mb": 40960,
      "mem_used_mb": 20000,
      "mem_percent": 48.83,
      "temp_c": 62,
      "power_w": 250,
      "health": "OK"
    }
  ],
  "gpus": [],
  "npus": [],
  "gpu_processes": []
}
```

无卡、读不到的利用率或负载用空列表或 null，不用 0 冒充「没有这块数据」。PCI 回退卡可以没有 `util_percent`。

### 5.2 存储要点

- `hosts`：身份、最新快照、在线判定、隐藏标记、资产 JSON（含历史 IP 与改址撤销）。补报若比当前快照更旧，不改 `last_seen` 和末次快照
- `metrics_history`：历史上报。`ts` 是采集时间，`received_at` 是中心收到的时刻，`backfill` 标记补报。同一 `(host_id, ts)` 只留第一笔。保留期只清这张表。历史点可带精简 `disks[]`（挂载、百分比、已用、总量）
- `spool_overflows`：采集端上报的队列溢出。被丢掉的时段在历史里仍是「离线（无数据）」
- `diagnostic_reports`：周期、半开请求窗、是否裁剪、Markdown、JSON、`source`、`status`、`error`、`generated_at`。清理历史点时不删
- 资源组、部署清单、`admin_settings`：只在中心库。部署目标可标 `retired`

---

## 6. 接口需求

写接口在启用管理 Token 后要带 `X-Management-Token`。未设置时与不校验时相同。下表「写」指会改中心状态的调用。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/metrics` | Agent 实时上报。校验 Agent `token`。响应带 `spool_retention_hours` |
| POST | `/api/v1/metrics/batch` | 补报。正文 `{samples, overflows, token, host_id}`，单次最多 100 条。同样只校验 Agent `token` |
| GET | `/api/v1/health` | 健康检查，含中心 `version` |
| GET | `/api/v1/hosts` | 主机列表。默认不含已隐藏。`?hidden=1` 只看已隐藏，`?include_hidden=1` 含全部 |
| GET | `/api/v1/hosts/{host_id}` | 详情，含 `asset` 与采集规格。已隐藏的仍可按 id 打开 |
| POST | `/api/v1/hosts/{host_id}/meta` | 更新手工资产。`previous_ips: []` 清空历史 IP；不传该键则保留 |
| POST | `/api/v1/hosts/{host_id}/hide` 与 `/restore` | 隐藏 / 恢复。隐藏正文可带 `remove_deploy_target` |
| DELETE | `/api/v1/hosts/{host_id}?confirm=delete` | 硬删除主机与历史。没有 `confirm=delete` 则 400 |
| POST | `/api/v1/hosts/meta/import` | 按 IP 预览或写入资产。`apply=1` 才写入 |
| POST | `/api/v1/hosts/{host_id}/readdress` | 同机更换展示 IP。默认预览 |
| POST | `/api/v1/hosts/{host_id}/readdress/undo` | 撤销上一次改址。默认预览 |
| GET | `/api/v1/hosts/{host_id}/history` | 趋势 |
| GET | `/api/v1/hosts/{host_id}/period-stats` | 单机时段统计 |
| GET | `/api/v1/period-stats` | 集群时段统计 |
| GET | `/api/v1/export/period-stats.csv` | 导出当前时间窗时段报表。表头为中文，数值不变 |
| GET | `/api/v1/stats` | 集群即时统计 |
| GET | `/api/v1/anomaly` | 异常判定汇总 |
| GET | `/api/v1/export/hosts.csv` 与 `.json` | 导出当前主机快照。CSV 表头为中文；JSON 保持英文键给程序用 |
| GET/POST/DELETE | `/api/v1/groups` 与 `/api/v1/hosts/{id}/group` | 资源组 |
| GET | `/api/v1/reports/preview` | 只返回将使用的时间窗，不计算、不入库 |
| POST | `/api/v1/reports/generate` | 生成并保存。同一窗口覆盖。HTTP 生成一律 `manual` |
| GET | `/api/v1/reports` 与 `/api/v1/reports/{id}` | 列表与详情（含 Markdown） |
| GET | `/api/v1/reports/{id}/markdown` 与 `.../rankings.csv` | 下载。排行 CSV 表头为中文 |
| GET | `/api/v1/export/field-guide.txt` 与 `hosts-fields.csv`、`period-stats-fields.csv`、`rankings-fields.csv` | 汉字字段说明（含义和单位）。不要把说明行塞进数据 CSV 的表头下面 |
| GET/POST | `/api/v1/settings` | 管理 Token、诊断报告推送和最近一次推送结果。GET 不回显 Token 明文和 Webhook 原文 |
| POST | `/api/v1/settings/push-test` | 「测试发送」。不生成报告。未启用或没有 Webhook 时不访问网络。与其它写接口同一套管理 Token |
| GET/POST | `/api/v1/data-guard` | 重点数据守护。GET 列出主机、资源组和已保存策略；POST 保存或清除，要管理 Token。上报响应里的 `data_guard` 是下发给该采集端的策略 |
| * | `/api/v1/deploy/*` | 部署清单、设置、任务、安装脚本、模板 |
| GET | `/`、`/deploy.html`、`/settings.html` | 监测台、部署、设置 |

再次 GET 报告不会重算。窗口已知但计算失败时 HTTP 400，响应里仍带 `status=failed` 的记录。

---

## 7. 现行范围之外

下面这些不是本产品的现行能力。已经交付的页面着色、使用诊断和报告 Webhook 不在此列。

- 把 Prometheus、Grafana、Zabbix 或其他监控栈装进运行依赖
- 按阈值即时通知（邮件、短信、电话）和自动修复。黄/红只是页面着色。今日关注、离线、数据外传异常都不即时推送；诊断报告推送只在报告生成成功或手动「测试发送」时发出
- 双中心、双活、SQLite 多写、自动故障切换
- 按用户拆分的监控中心，或把管理 Token 做成多用户登录
- 容器 / Kubernetes 深度监控、应用 APM
- 通用时序图表引擎。日/周/月报告是固定口径的文字报告
- 无 Agent 的自动发现
- 给 `push_upgrade.sh`、`push_python_bundles.sh` 增加 `--dir`，或把 `~/monitor` 当成生产升级目标
- 指标框架化（N2 / NH-04）。单独跟踪 [Issue #35](https://github.com/lcw918110/monitor/issues/35)，不在本轮实现
- 总览里的长期空闲名单、把部署页收进同一页、在浏览器里执行中心升级。长期空闲仍只看使用诊断
- 把重点数据守护做成 DLP 或拦截：不读文件内容，不抓包，不丢连接，也不能证明数据没有离开

---

## 8. 验收标准

1. 在 `/opt/monitor` 启动生产中心后，浏览器可打开监测台、部署页和设置页。`push_upgrade.sh` 只针对该目录，试用目录不走这条命令。
2. 至少一台 `cpu` 主机和一台带加速卡（或 PCI 回退 / 模拟卡）的主机完成上报。无卡主机仍能上报系统指标。
3. 列表显示在线状态和完整地址。离线主机不把末次快照当成当前利用率。详情能看到 CPU、内存、磁盘和加速卡（有则显示），手工资产不被上报覆盖。
4. 隐藏后该机退出列表和在线统计；恢复后重新出现。没有 `confirm=delete` 时硬删除不发生。
5. 时段统计能按相对分钟和绝对起止返回 avg / min / max / p95 / busy_ratio，集群为样本加权。
6. 使用诊断能生成并再次打开日/周/月报告。窗口里没有上报时，三份报告都留在列表里，页面标明无数据，不消失。基本不用必须同时满足平均 <5% 和繁忙 <5%（有卡时加速卡同样）。平均 14% 或加速卡平均 9% 不是基本不用。高负荷为样本足够且繁忙占比 ≥40% 或 P95 ≥85%。
7. 停止 Agent 超过离线阈值后状态变为离线，今日关注把它算进离线。实时页不出现第二种离线徽章。采集端在送不出去时把样本留在 `data/spool`，恢复后按采集时间补报；时段统计把补上的时段写成「断网（已补报）」，没有样本的空档写成「离线（无数据）」。
8. 未设置管理 Token 时写接口可用；设置后，不带 `X-Management-Token` 的写操作被拒绝，读报告仍可用。保存 Webhook 和启用推送不依赖管理 Token。未配置 Webhook 时生成报告只入库，「测试发送」不访问网络。推送失败时报告仍然成功。设置页能看到最近一次推送结果，且看不到 Webhook 原文。
9. 全程无外部监控软件依赖。自测 `PYTHONPATH=. python3 -m unittest discover -s tests` 通过。
10. 未标记的主机不出现重点数据机徽章，上报里的重点数据字段也不入库。标记后，路径变小、大归档、允许网段之外的连接、外传阈值和可移动磁盘会进入「数据外传异常」；白名单中的备份目的或进程不报。报文中没有文件内容。

---

## 9. 需求追踪

| 需求 | 实现 | 主要测试 |
| --- | --- | --- |
| FR-01 | `scripts/deploy_center.sh`、`push_upgrade.sh`、`upgrade_center.sh`、`center/storage.py` | `tests/test_upgrade_center.py`、`tests/test_center_backup.py` |
| FR-02 | `agent/main.py`、`agent/spool.py`、`agent/metrics/` | `tests/test_basic.py`、`tests/test_spool_backfill.py`、`tests/test_gpu.py`、`tests/test_amd.py`、`tests/test_npu.py`、`tests/test_cnmon.py`、`tests/test_rknn.py`、`tests/test_pci_accel.py`、`tests/test_disk.py`、`tests/test_network.py`、`tests/test_load.py` |
| FR-03、FR-04 | `center/api.py`、`center/hostmeta.py`、`center/readdress.py`、`center/anomaly.py`、`center/static/` | `tests/test_host_meta.py`、`tests/test_host_list.py`、`tests/test_readdress.py`、`tests/test_ux.py` |
| FR-UTIL | `center/storage.py` 时段聚合 | `tests/test_period_util.py` |
| FR-DIAG | `center/reports.py`、`center/report_schedule.py` | `tests/test_reports.py` |
| FR-ADMIN | `center/admin_settings.py`、`center/report_push.py` | `tests/test_admin_settings.py` |
| FR-DEPLOY | `center/deploy_*.py`、`scripts/deploy_agent.sh`、`scripts/deploy_fleet.sh` | `tests/test_deploy.py`、`tests/test_python_bundle.py` |
| FR-GUARD | `common/data_guard.py`、`agent/metrics/data_guard.py`、`center/data_guard.py` | `tests/test_data_guard.py` |

操作步骤以 `docs/14-ops-runbook.md` 为准。报告字段和调度细节以 `docs/13-diagnostic-reports.md` 为准。下一步里尚未交付的项以 `docs/10-roadmap.md` 为准。
