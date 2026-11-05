# 对外部署手册

本文给准备在自己机房安装「简易多机监控」的系统管理员。读者不需要先看过仓库里的其它记录。步骤按现行 **v1.10.0** 编写，和仓库根目录 [`README.md`](../README.md)、[`14-ops-runbook.md`](14-ops-runbook.md) 一致。升级、备份和回滚的细节以运行手册为准；和脚本不一致时以脚本为准，并应改文档。

文中的 `<中心IP>`、`root@<中心>`、`http://<中心IP>:8080` 都要换成现场地址。生产中心目录固定为 `/opt/monitor`。升级脚本和便携包脚本没有 `--dir`，只作用于这一目录。

## 1. 文档说明与适用范围

本系统是轻量的多机资源监控：中心收数，采集端上报，浏览器看页面。它看 CPU、内存、磁盘、网络和加速卡用得怎样、是否偏高，并做时段统计和日/周/月使用诊断。

它不是 Prometheus、Grafana、Zabbix 那一套监控栈。运行时不安装这些软件，也不依赖第三方 pip 包，用 Python 3 标准库实现。页面黄/红是阈值着色，不是邮件、短信或电话告警。诊断报告可以选配推送到企业微信、钉钉或飞书，见 [`17-external-user-guide.md`](17-external-user-guide.md)。

现行边界还有这些：

- 全局一套中心，不分用户各装一套。没有按人拆开的账号体系。
- 不做双中心、双活、SQLite 多写或自动故障切换。备份和恢复由人执行。
- 不自动发现主机。要监控的机器需要装上采集端。
- 不捆绑厂商加速卡驱动或工具库。目标机上有工具就采，没有工具时按第 3 节的 PCI 回退处理。
- 本文只写生产安装。仓库里的本机试用脚本 `scripts/local_up.sh` 不是交付路径，见 README「本地试用」。

能单独阅读的需求正文在 [`01-requirements.md`](01-requirements.md)。架构图在 [`02-architecture.md`](02-architecture.md)。

## 2. 架构一览

```text
浏览器 ── HTTP ──▶ Center  /opt/monitor     端口默认 8080
                      ▲
                      │  POST /api/v1/metrics
        Agent /opt/monitor-agent   （每台被监控机器一份）
```

| 角色 | 做什么 | 生产目录 | 进程 |
| --- | --- | --- | --- |
| Center（中心） | 收指标、写入 SQLite、提供页面和 API | `/opt/monitor` | systemd 单元 `monitor-center` |
| Agent（采集端） | 按间隔采集并主动上报 | `/opt/monitor-agent`（非 root：`~/monitor-agent`） | systemd 单元 `monitor-agent` |

中心默认监听 `0.0.0.0:8080`。页面入口：

| 页面 | 地址 |
| --- | --- |
| 监测台 | `http://<中心IP>:8080/` |
| 客户端部署 | `http://<中心IP>:8080/deploy.html` |
| 设置 | `http://<中心IP>:8080/settings.html` |

数据在中心安装目录的 `data/monitor.db`（首次安装脚本把 `data_dir` 写成该目录的绝对路径）。配置在 `config/center.json`。

同机既跑中心又跑采集端时，两个目录必须分开：中心保持 `/opt/monitor`，采集端用 `/opt/monitor-agent`。中心安装树里必须保留 `agent/` 包，因为网页部署和 `deploy_fleet.sh` 从这里打采集端包（`agent/`、`common/`、`scripts/` 与配置模板），不会把 `center/` 整树拷到目标机。

采集端主动访问中心，所以目标机只要能出站访问中心 URL。中心在部署时要能 SSH 到目标机。

## 3. 环境与前置条件

### 3.1 中心机

| 项 | 要求 |
| --- | --- |
| 操作系统 | Linux。生产安装使用 root |
| Python | 3.8 及以上。中心不使用给采集端准备的便携包 |
| 服务管理 | 推荐 systemd。`deploy_center.sh` 在 root 且本机有 `systemctl` 时安装 `monitor-center.service` |
| 出站 SSH | 中心机要能 SSH 到每台目标机。端口默认 22，不扫描 |
| 密码部署 | 用密码登录目标机时，**中心机**要已安装 `sshpass`。没有就改用 SSH 私钥。缺少时失败短码是 `sshpass_missing` |
| 入站 | 运维浏览器和各采集端要能访问中心的 TCP 端口（默认 8080） |
| 时钟 | 样本按 Unix 时间保存。诊断报告的日界和定时时刻按 Asia/Shanghai（UTC+8）计算，不跟操作系统时区改口。中心机时钟仍应准确 |
| 磁盘 | 历史上报会随主机数、采样间隔和 `retention_days` 增长。升级前还要留出大约 3 倍 `data/monitor.db` 再加 20MB。能撑多少台见第 13 节，本文不写主机上限 |

不以 root 执行 `deploy_center.sh` 时，脚本会把目录改成 `~/monitor`，并跳过 systemd、改用后台进程。那不是生产安装。`push_upgrade.sh` 不会升级家目录里的那一份；它只找 `/opt/monitor`。目录不存在就退出；若 `/opt/monitor` 是另一套安装，则会升级那一套。

### 3.2 采集端目标机

| 项 | 要求 |
| --- | --- |
| Python | 3.6 及以上。Ubuntu 16.04 自带的 3.5.2 不够 |
| 解释器顺序 | 先复用本机已有的 `PYTHON_BIN`、`python3.x` 或 `/usr/local/python3.*`（都要 ≥ 3.6）；都没有且为 root 时再尝试 apt/yum/dnf 安装 python3；安装失败不中断，继续使用中心上传的便携包 |
| 网络 | 目标机要能访问「中心对外访问地址」，例如 `http://<中心IP>:8080`。这个地址不要写成 `127.0.0.1` |
| 安装目录 | 新产品默认 `/opt/monitor-agent`。非 root 写入 `/opt/...` 时，产品路径先 `sudo -n`，不行再用同一次 SSH 的密码做 `sudo -S`。仍然不行则短码 `sudo_required`。本机直接跑脚本且没有 sudo 时，会落到 `~/monitor-agent` |
| SSH | 中心机用密码或私钥登录。端口按清单填写，默认 22 |

### 3.3 加速卡工具（可选）

产品不随包提供厂商库。目标机上已有工具时才读取利用率、温度、显存等。厂商和工具：

| 厂商 | 工具 |
| --- | --- |
| 英伟达 | `nvidia-smi`（版本看 `-h` 抬头） |
| AMD | 优先 `rocm-smi`，失败再 `amd-smi` |
| 华为昇腾 | `npu-smi`。利用率只接受 0～100 |
| 寒武纪 | `cnmon`。文本按整行 `Card N` 分卡；`Good` 视为正常 |
| 瑞芯微 RKNN | rknpu 相关节点 |

没有厂商工具、也没有对应设备节点时，采集端从 `/sys/bus/pci/devices` 识别已知加速卡（例如寒武纪 `cabc:0270`）。这时健康状态为 `abnormal`，页面用白话说明驱动缺失或设备无响应，利用率留空，**不会**当成 0% 样本。认不出设备则加速卡列表为空。两种情况都不影响 CPU、内存、磁盘等系统指标的上报。

同一块卡按 PCI 总线号合并。工具失败时，stderr 或退出码写进该卡的说明。主机类型 `cpu` 不采加速卡；`gpu` 与 `auto` 会采，`auto` 有卡则记为 `gpu`。

## 4. 首次安装中心

生产中心的首次安装只有这一条：在中心机上以 root 执行 `scripts/deploy_center.sh`，目录 `/opt/monitor`。

不要用 `scripts/upgrade_center.sh` 做第一次安装。安装目录还不存在时，升级脚本会直接退出。已经装好的中心以后换代码走第 8 节，不要反复用本节脚本当日常升级。`deploy_center.sh` 会保留已有的 `config/center.json` 和 `data/`，但它没有「同步前先跑测试，失败则只回滚代码」的那一套。

在中心机上，进入已经拿到的源码目录：

```bash
cd <源码目录>
chmod +x scripts/*.sh
./scripts/deploy_center.sh --port 8080
```

可选参数：

| 参数 | 作用 |
| --- | --- |
| `--port 8080` | 监听端口，默认 8080 |
| `--host 0.0.0.0` | 监听地址，默认所有网卡 |
| `--token '<上报口令>'` | 仅在**还没有** `config/center.json` 时写入 Agent 上报 Token。这不是第 5 节的管理 Token |
| `--offline-seconds 90` | 多少秒没有上报算离线，默认 90 |
| `--no-systemd` | 不安装 systemd。生产环境不建议 |
| `--no-start` | 只安装不启动 |
| `--dir /opt/monitor` | 生产目录。root 下默认就是它。不要改到别的路径当作生产中心 |

脚本会同步代码、在没有配置文件时生成 `config/center.json`、安装并启用 `monitor-center`，然后在本机请求 `http://127.0.0.1:8080/api/v1/health`。这里的 `127.0.0.1` 只用于中心机自己检查进程，不是给远端采集端的地址。

成功后在运维电脑上打开 `http://<中心IP>:8080/`。健康检查应返回 JSON，其中 `ok` 为 true，并带 `version` 和 `name`。

首次生成的 `center.json` 里，历史上报保留天数 `retention_days` 是 7，诊断调度按第 8 节所说的默认时刻写好。加速卡阈值在这份初始文件里用的是旧键名 `npu_*`。中心仍然认识这些键；和 `accel_*` 同时存在时以 `accel_*` 为准。设置页可以把单项恢复成现行默认。字段说明见 [`../config/center.example.md`](../config/center.example.md)。

systemd 的日志用：

```bash
systemctl status monitor-center
journalctl -u monitor-center -n 50 --no-pager
```

没有使用 systemd 时，日志在 `/opt/monitor/run/center.log`。

## 5. 管理 Token 与基础设置

打开 `http://<中心IP>:8080/settings.html`。这一页编辑 `config/center.json` 里的中心参数，以及另外两项：管理 Token、诊断报告推送。除监听地址、端口、数据目录需要重启 `monitor-center` 以外，其余保存后当前进程立即生效。

在中心端口对他人可见之前，建议先启用管理 Token。未设置时，能访问该端口的人可以部署、改主机、生成报告和改设置。读监测台不需要这把口令。

两把口令不能互换：

| 口令 | 存在哪里 | 用来做什么 |
| --- | --- | --- |
| 管理 Token | 中心库表 `admin_settings`，只存 SHA-256，不写入 `center.json` | 保护写接口。请求头 `X-Management-Token: <明文>`。页面和 `GET /api/v1/settings` 不再给出明文 |
| Agent 上报 Token | `config/center.json` 的 `token`。设置页不回显明文 | 只校验 `POST /api/v1/metrics`。留空表示上报不校验。不能用来改设置，也不是网页登录 |

管理 Token 至少 8 位、不含空格。也可以在**第一次启动前**设置环境变量 `MONITOR_MANAGEMENT_TOKEN`。环境变量只在库里还没有管理 Token 时写入哈希，不会覆盖已有值，也不会写进 `center.json`。短于 8 位时中心拒绝启动。

当前标签页用顶栏黄条记住明文，关掉标签页后要再填。清空管理 Token 也要带当前明文。忘记时，在中心机执行下面这一条，然后立刻重新设置：

```bash
sqlite3 /opt/monitor/data/monitor.db \
  "DELETE FROM admin_settings WHERE key='management_token_sha256';"
```

报告推送、保留天数和阈值的日常改法见使用手册。手改 `/opt/monitor/config/center.json` 之后要重启 `monitor-center` 才会重读文件；这只重读配置，不换代码，也不是升级。

部署用的 SSH 密码是另一回事，见第 6 节末尾。

## 6. 部署 Agent

采集端只有这一条产品路径：中心页面 `http://<中心IP>:8080/deploy.html`，或在**中心机**上执行 `scripts/deploy_fleet.sh` / `scripts/deploy_agent.sh`。`scripts/install_agent.sh` 只是 `deploy_agent.sh` 的兼容别名。

「中心对外访问地址」要写成远端机器能够访问的 URL，例如 `http://<中心IP>:8080`。页面会尝试填入中心机的局域网地址。不要填 `127.0.0.1`：那是目标机自己，采集端会把数据报到错误的地方，失败短码多为 `agent_start_fail` 或 `center_url_missing`。

SSH 端口默认 **22**。清单、Excel 的 `ssh_port`、网页「SSH 端口」、`deploy_fleet.sh --ssh-port` 或 `--port` 都可以指定。**不扫描端口。** 连接超时或连接被关闭时会自动重试 2～3 次，不会无限重试。

### 6.1 在部署页安装（推荐）

1. 打开客户端部署页。若已启用管理 Token，先在监测台顶栏填入并在本页使用。
2. 填写并保存「中心对外访问地址」。同时可保存默认 SSH 用户、默认端口 22、默认安装目录 `/opt/monitor-agent`、上报间隔（默认 15 秒，下限 5 秒）。
3. 认证二选一，也可以按机器单独覆盖：
   - 用户名 + 密码。中心机必须已安装 `sshpass`。
   - 用户名 + 私钥。全局「私钥路径」是多台机器共用的一把钥匙，文件必须在**中心机**上。某一台不同的钥匙或密码，在该客户端上单独填。
4. 添加目标，或下载 Excel 模板 `/api/v1/deploy/template.xlsx` 后导入。添加和导入时会对已填地址做探查，清单「网络」列给出结果。
5. 点「测试连通/部署条件」，确认 SSH 和安装目录可写。
6. 「部署勾选」或「全部部署」。成功时状态为「部署成功」。失败时「最近消息」为 `[短码] 说明`。

部署页上的「目标机补跑脚本」只在产品路径已经把代码同步到目标机之后，在该机再跑一次 `deploy_agent.sh`。它不是从别的电脑拷文件的第二条安装方式。

默认远端目录是 `/opt/monitor-agent`。清单里已经写成 `/opt/monitor` 的旧记录不会自动改写。同机中心仍占用 `/opt/monitor`，不要把新的采集端指到那个目录。

### 6.2 在中心机用脚本安装

单台（人已经在目标机上，或中心已经把代码同步过去之后）：

```bash
./scripts/deploy_agent.sh --center-url http://<中心IP>:8080
# 可选: --host-id gpu01 --token '<与中心 center.json 相同的上报口令>' \
#       --interval 15 --dir /opt/monitor-agent
```

批量在中心机上执行。先复制示例清单再改：

```bash
cp config/hosts.example.txt config/hosts.txt
# 编辑 IP、host_id、可选 ssh_port（默认 22）
./scripts/deploy_fleet.sh \
  --hosts-file config/hosts.txt \
  --center-url http://<中心IP>:8080
# 非 22 端口：--ssh-port 2222 或 --port 2222
# Agent 目录：--remote-dir /opt/monitor-agent
```

清单每行格式是 `IP[:port] [host_id] [ssh_port]`。行内端口覆盖 `--ssh-port`。示例里的地址是虚构的，见 [`../config/hosts.example.txt`](../config/hosts.example.txt)。

脚本在目标机上的顺序是：解析 Python，同步采集端文件（优先 rsync，否则 tar 管道或 cp），写入 `config/agent.json`，探测加速卡工具，做一次上报自检，然后用 systemd 或守护进程常驻。覆盖安装会先停旧的 `monitor-agent`，再同步；**不删除**目标机上已有的 `config/agent.json` 与 `data/`，本轮仍按中心下发的参数写入 `agent.json`。

采集端使用 systemd 时，在目标机查看：

```bash
systemctl status monitor-agent
journalctl -u monitor-agent -n 50 --no-pager
```

### 6.3 部署失败短码

成功只显示「部署成功」。失败为 `[短码] 说明`。下表与 README「部署失败短码」一致。

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

### 6.4 SSH 密码如何保存

部署页保存的默认 SSH 密码，以及每台目标自己的 SSH 密码，以**明文**写在中心 SQLite 里（`deploy_settings` 与 `deploy_targets.ssh_password`），供中心下次 SSH 使用。页面再次打开时不再回显明文，留空表示不改已保存的密码。`config/center.json` 里的 Agent 上报 Token 也是文件中的明文。管理 Token 只存哈希，和这两处不同。

因此 `/opt/monitor/data/monitor.db` 和每一份备份都含有这些 SSH 密码。请限制中心机和备份目录的访问权限。资产表导入会丢掉列名里带用户名、密码、口令、password、token 的整列，那些列不会进入主机资产；部署清单是另一张表，不受这条保护。

## 7. 便携 Python

目标机没有 Python ≥ 3.6 时，使用中心上的便携 CPython。约 26MB 的 tar.gz **不要提交进 git**（`.gitignore` 已排除 `vendor/python/*.tar.gz`）。拿到的源码包里通常没有这些文件，需要单独放置。

在一台**能访问互联网**的机器上，于源码目录执行：

```bash
scripts/fetch_python_bundle.sh --dest vendor/python
scripts/push_python_bundles.sh root@<中心>
```

非交互密码放在环境变量里，不写进命令行：

```bash
SSHPASS='***' scripts/push_python_bundles.sh root@<中心>
```

固定包是 python-build-standalone `20241016` 的 **install_only**（不是 musl、stripped 或 `gnu_2_28`），glibc ≥ 2.17，包含 x86_64 与 aarch64。中心按目标机 `uname -m` 选择。其它架构会得到短码 `python_bundle_arch`。

`push_python_bundles.sh` 的远端目录固定是 `/opt/monitor/vendor/python`，没有 `--dir`。父目录不存在时会先创建。中心升级不会删掉这里已有的 tar.gz。

网页部署和 `deploy_fleet.sh` 在目标机没有合适解释器时上传并解压：root 解到 `/usr/local/monitor-python`，否则解到采集端安装目录下的 `python/`。

目录也可以在 `config/center.json` 里改成现场路径。示例：

```json
"deploy": { "python_bundles": "/opt/monitor/vendor/python" }
```

不写这一键时，有 `/opt/monitor/vendor/python` 就用它，否则用源码树里的 `vendor/python`。环境变量 `MONITOR_PYTHON_BUNDLES` 优先于配置。说明见 [`../config/center.example.md`](../config/center.example.md)。

## 8. 升级已安装中心

已经装好的 `/opt/monitor` 只从**有这份 git 仓库的工作站**升级。中心上执行的是**这次推送包里**的 `scripts/upgrade_center.sh`，不是中心上的旧脚本。

```bash
# 只检查，不改 /opt/monitor
scripts/push_upgrade.sh --check-only root@<中心> HEAD

# 正式升级
scripts/push_upgrade.sh root@<中心> HEAD

# 非交互密码留在环境变量里
SSHPASS='***' scripts/push_upgrade.sh root@<中心> HEAD
```

`MONITOR_PUSH_DRY=1` 只打印将使用的 SSH 命令，不连接。设置了 `SSHPASS` 但工作站没有 `sshpass` 时，脚本退出，不会改用普通 `ssh`。

工作站把该 git 版本解到中心 `/tmp/monitor-upgrade-src`，然后执行：

```bash
bash /tmp/monitor-upgrade-src/scripts/upgrade_center.sh \
  /tmp/monitor-upgrade-src --sha <该版本 sha> --dir /opt/monitor
```

`--check-only` 时在命令末尾再加 `--check-only`。这条命令只升级 `/opt/monitor`。家目录 `~/monitor` 和试用目录都不走这里。

中心上的升级脚本接着做这些事。完整顺序见 [`14-ops-runbook.md`](14-ops-runbook.md)。

1. `/opt/monitor` 或其中的 `config/center.json` 不存在则退出，不生成空配置。
2. 从现有 systemd 单元读出 `LD_LIBRARY_PATH` 和 `PATH`，再选 Python。测试在**新包**上跑，标准输入接到 `/dev/null`。
3. 测试失败，或磁盘可用空间不够「约 3 倍 `data/monitor.db` 再加 20MB」，则退出。此时还没有改安装目录。
4. `--check-only` 在测试和磁盘预检通过后打印「检查通过（未改动安装目录）」并退出。它不会把便携包写入现场，也不备份、不同步、不重启。
5. 正式升级用 SQLite `backup` API 做一致副本并 `quick_check`。然后按 `retention_days` 清理过期历史上报（写 0 则这次不删），再同步代码。
6. 同步**保留** `config/center.json`、`data/`、`vendor/python` 里已有的 tar.gz。会重写 `monitor-center.service`，但保留原来的 `ExecStart` 和已有的 `Environment=`（含 `PATH`、`LD_LIBRARY_PATH`）。
7. 重启后检查本机 `/api/v1/health`、`/api/v1/hosts`、`/api/v1/reports`。

回滚边界：

| 情况 | 代码、配置、单元 | 数据库 `data/monitor.db` |
| --- | --- | --- |
| 测试或磁盘预检失败 | 现场不动 | 现场不动 |
| 已经同步，健康检查失败 | 自动回到升级开始前的代码和单元。失败代码留在 `/opt/monitor-failed-<时间>` | **不覆盖**。备份里的库仍在备份目录 |
| 人执行 `restore_center.sh --code-only` | 回到指定备份 | 不覆盖 |
| 人执行 `restore_center.sh --with-data` | 回到指定备份 | 覆盖为备份当时的库。备份之后的新上报、新报告、新部署清单都不再保留 |

代码回滚不等于数据库回滚。升级脚本自己的失败回滚不动 `data/`。要把库也退回去，必须另做第 9 节的 `--with-data`，并接受丢掉备份之后写入的数据。

`--keep-backups` 默认保留 5 份，目录形如 `/opt/monitor-backup-<时间>`。下一次升级会把 `backup_center.sh` 单独做的备份和升级备份放在一起，只留最新 5 份。`push_upgrade.sh` 目前不转发保留份数。

只改配置、不换代码时，用设置页，不要跑升级脚本。

不要把 `scripts/lib/sync_tree.sh` 当成升级命令。它是函数库，直接执行不会同步代码，退出码为 1。也不要手写 `rsync` 或 `cp` 再 `systemctl restart monitor-center`。重启不会换上新代码。

## 9. 备份与恢复

不升级时，在中心机做一份备份。这不是第二条升级路径，也不能用来安装新版本。

```bash
# 在中心机上。默认目录 /opt/monitor。中心可以继续跑
/opt/monitor/scripts/backup_center.sh --dir /opt/monitor

# 做完后只留最新 5 份（可选；不传 --keep 就不删旧的）
/opt/monitor/scripts/backup_center.sh --dir /opt/monitor --keep 5
```

备份使用 SQLite 的 `backup` API，不修改现场库。磁盘不够（仍是约 3 倍数据库再加 20MB）时直接退出，不留下半份目录。

目录在安装目录旁边：

```text
/opt/monitor-backup-YYYYMMDDHHMMSS/
  code/                          当时的安装树
  code/config/center.json        配置，含上报 Token、retention_days、端口
  code/data/monitor.db           一致副本（含主机、历史上报、诊断报告、资源组、部署清单和 SSH 密码）
  code/vendor/                   便携 Python 等（现场有才有）
  monitor-center.service         单元文件副本（没有 systemd 时可能没有这个文件）
  backup-manifest.json           仅 backup_center.sh 写入。升级产生的备份可以没有这个文件
```

`retention_days` 只清理历史上报，不删除已生成的诊断报告。整库恢复会把这些表一起带回备份当时的内容。

恢复必须写明模式，并加 `--yes`：

```bash
# 只回到备份里的代码和配置，数据库保持现场
/opt/monitor/scripts/restore_center.sh \
  --from /opt/monitor-backup-YYYYMMDDHHMMSS \
  --dir /opt/monitor \
  --code-only --yes

# 连数据库一起回到备份当时。会先停止 monitor-center
/opt/monitor/scripts/restore_center.sh \
  --from /opt/monitor-backup-YYYYMMDDHHMMSS \
  --dir /opt/monitor \
  --with-data --yes
```

`--with-data` 在换库之前要求进程已经停掉。脚本会先 `systemctl stop monitor-center`。停不掉就退出，此时还没改目录。没有 systemd 时，自己先停进程，再加 `--no-systemd --assume-stopped`。不需要写回单元文件时加 `--no-unit`。恢复之后，若没有加 `--no-systemd`，脚本会 `systemctl restart monitor-center`。

备份目录不是热备。不要把流量切到备份目录，也不要在备份目录里再启动一套中心。

## 10. 卸载

在**已经安装该角色的那台机器**上执行安装目录里的 `scripts/uninstall.sh`。

```bash
# 在目标机上，停止采集端。默认 /opt/monitor-agent（非 root 时为 ~/monitor-agent）
/opt/monitor-agent/scripts/uninstall.sh --role agent

# 在中心机上，停止中心并删除 data 与 config。代码目录仍保留
/opt/monitor/scripts/uninstall.sh --role center --purge-data

# 旧采集端若仍装在 /opt/monitor：必须写明目录，避免卸错中心
./scripts/uninstall.sh --role agent --dir /opt/monitor
```

root 且本机有 systemd 时，脚本会停止并禁用对应单元（`monitor-center` 或 `monitor-agent`），并删除 `/etc/systemd/system/` 下的单元文件。不加 `--purge-data` 时只停服务，保留 `data/` 和 `config/`。加上之后删除 `data/`、`config/` 和 `run/`，代码目录还在。

卸载**不会**删除旁边的 `/opt/monitor-backup-<时间>`。那些目录里仍有数据库和 SSH 密码，不再需要时由管理员自行删除。

中心部署清单里的目标不会因为卸掉某一台采集端而自动消失。下线机器在监测台详情里隐藏，或在部署清单里停用。见使用手册。

## 11. 验收检查清单

在中心机和一台运维浏览器上逐项做。把 `<中心IP>` 换成现场地址。管理 Token 启用后，写操作要带请求头；下面的检查都是读接口，不需要这把口令。

1. **健康检查。** 中心机上：

   ```bash
   curl -sS http://127.0.0.1:8080/api/v1/health
   ```

   另一台能访问中心的机器上：

   ```bash
   curl -sS http://<中心IP>:8080/api/v1/health
   ```

   两边都应看到 `"ok": true`，以及 `version`。本机这一条里的 `127.0.0.1` 不能抄进采集端的中心地址。

2. **主机接口。** `curl -sS http://<中心IP>:8080/api/v1/hosts` 返回 JSON，内有 `hosts` 数组。还没有上报时数组为空，这是预期。

3. **页面。** 浏览器打开 `http://<中心IP>:8080/`、`/deploy.html`、`/settings.html`，三页都能打开。

4. **部署成功。** 至少一台目标机在部署清单上显示「部署成功」，没有失败短码。目标机上 `systemctl status monitor-agent` 为 active（使用 systemd 时）。

5. **样本上报。** 等待一个上报间隔（默认 15 秒）之后，监测台「实时监控」出现该主机，状态为在线。离线阈值默认约 90 秒。详情里能看到 CPU、内存、磁盘；有加速卡或 PCI 回退卡时能看到对应说明。离线主机不会把最后一次数字显示成当前利用率。

6. **报告预览。** 打开监测台「使用诊断」。若上一完整周期还没有样本，预览时间窗可以打开，生成可能因窗口超出保留期或没有数据而失败并留下失败记录，这不表示中心没装好。已有足够样本时，`GET /api/v1/reports/preview?period=day` 只返回将使用的时间窗，不入库。需要入库时再在页面点「生成并保存」。口径见 [`13-diagnostic-reports.md`](13-diagnostic-reports.md)。

7. **上报口令。** 若 `center.json` 的 `token` 非空，采集端配置里的 `token` 必须相同，否则上报被拒绝，部署自检短码为 `agent_start_fail`。

不要在生产库上执行 `scripts/demo_seed.py`。它会写入演示主机，只适合准备丢掉的本地库。

## 12. 明确禁止项

- 在笔记本或其它旁路机器上用 `sshpass` 和 `scp` 安装采集端。那样会和中心清单、安装目录、失败短码脱节。
- 把采集端装进中心的 `/opt/monitor`，覆盖中心。采集端默认目录是 `/opt/monitor-agent`。
- 用手写 `rsync`、`cp`、`git archive` 再 `systemctl restart` 当作升级。升级只用 `scripts/push_upgrade.sh`。
- 直接执行 `scripts/lib/sync_tree.sh`，或用 `restore_center.sh` 发布新版本。
- 做第二套中心、SQLite 多写，或自动把访问切到备份目录。
- 升级失败后用备份目录覆盖 `data/`。自动回滚故意不动数据库。退库必须人执行 `--with-data`。
- 对 `~/monitor` 或试用目录执行 `push_upgrade.sh`。
- 在容量按 [`15-capacity-lab.md`](15-capacity-lab.md) 于现场测完之前，对外承诺主机台数上限。
- 往生产中心灌 `scripts/demo_seed.py`。

## 13. 容量与保留期

历史上报保留天数是 `retention_days`。首次安装默认 **7**。可以在设置页改长，例如现场需要月报时改成 40。保存后当前进程立即按新值清理。清理只删除过期的 `metrics_history`，**不删除**已经生成的诊断报告。设为 0 表示不按天数删除，诊断窗口也不再被裁成 7 天。

报告所用的时间窗若早于保留期，抬头会写明裁剪（clamped）。整段都在保留期之外时，生成失败，不编造利用率。因此要看月报时，保留天数需要盖住那个月。

采样间隔默认 15 秒（部署页「上报间隔」，下限 5）。间隔越短、主机越多、保留越久，库越大。升级前的磁盘预检按大约 3 倍当前库体积再加 20MB 估算。

本系统是单进程加一个 SQLite。能同时带多少台、库会长到多大，必须在客户自己的中心机上用 `scripts/capacity_probe.py` 实测。方法、旋钮和结果表在 [`15-capacity-lab.md`](15-capacity-lab.md)。**本文不写主机上限，也不把仓库里已有的开发机实测行当作现场结论。** 探针拒绝往 `/opt/monitor` 写实验室库，应使用空的临时目录。冒烟运行只证明命令能结束，不能填进结果表。
