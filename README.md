# 简易多机监控系统

> **这是一个超级简单的服务器资源基本监控项目。**  
> 不做 Prometheus / Grafana / Zabbix 那一套完整监控栈，只解决「多台机器的 CPU、内存、磁盘、加速卡用得咋样、是否偏高」这类基本看数需求。

不引入外部监控软件，用 **Python 3 标准库** 实现多机 **CPU / 加速卡** 资源监测、时间段统计、异常判定与客户端批量部署。

当前版本：**v1.4.0**

运行环境：

| 角色 | Python |
| --- | --- |
| **Agent（采集端）** | **3.6+**（部署时优先复用本机已有解释器，没有再用 apt/yum/dnf 安装） |
| Center（中心端） | 3.8+ |

## 定位

- **项目性质**：轻量、可本地一把梭部署的**基础资源监控**，适合实验室 / 小机房快速看一眼；不是企业级可观测平台
- **监测**：CPU（利用率 / 核数 / 负载 / 架构 / 机型）+ 加速卡（英伟达 / 华为昇腾 / 寒武纪 / 瑞芯微 RKNN）
- **主机类型**：仅 `auto` | `cpu` | `gpu`（gpu=各类加速卡；兼容旧值 `app`→cpu、`npu`→gpu）
- **异常判定**：默认阈值触发后页面 **黄色(偏高) / 红色(异常)** 着色（不做告警通知通道）
- **详情统计**：可选时间段（1h / 2h / 6h / 24h / 7d 或自定义起止）的平均 / 最低 / 最高 / P95 / 繁忙占比；支持单机与集群汇总
- **部署**：中心「客户端部署」页或中心机上的 `deploy_fleet.sh` / `deploy_agent.sh`（唯一产品路径；禁止笔记本旁路 scp/sshpass）

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

中心安装树（`/opt/monitor`）**必须保留 `agent/` 包**：网页 SSH / `deploy_fleet.sh` 都是从中心目录打包下发。源树缺 `agent/` 时禁止同步（避免 `rsync --delete` 把已有包删掉），安装包缺 `agent/` 会短码 `package_incomplete`，不会再静默漏打。

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

脚本会：解析 Python（先复用本机 `PYTHON_BIN` / `python3.x` / `/usr/local/python3.*`，均需 ≥3.6；都没有且为 root 时再 apt/yum/dnf 安装 python3）→ 同步文件（**优先 rsync**，没有则 **tar 管道** 或 **cp -a**）→ 写配置 → 探测加速卡工具 → 单次上报自检 → systemd 或守护进程常驻。

内网常见情况：只有 3.6、PATH 里的 `python3` 过旧但 `/usr/local/python3.12` 可用、或完全没有 python3——脚本按「先复用、再安装」处理。自定义前缀若缺 libpython，会自动加上 `LD_LIBRARY_PATH`。

批量：

```bash
cp config/hosts.example.txt config/hosts.txt
# 编辑 IP / host_id / 可选 ssh_port（默认 22）
./scripts/deploy_fleet.sh --hosts-file config/hosts.txt --center-url http://<中心IP>:8080
# 非 22 端口：--ssh-port 2222  或  --port 2222；也可写在清单第三列 / IP:port
# Agent 目录：--remote-dir /opt/monitor-agent
```

清单格式：`IP[:port] [host_id] [ssh_port]`。行内端口覆盖 `--ssh-port`。SSH 连接超时 / connection closed 会自动重试 2～3 次（短退避），不会无限重试。

### 部署失败短码

成功仍只显示「部署成功」。失败为 `[短码] 说明`（网页清单与脚本日志相同）：

| 短码 | 含义 |
| --- | --- |
| `ssh_unreachable` | 连不上（超时、拒绝、无路由、DNS、连接被关闭）；先核对 IP 与 **ssh_port（默认 22）** |
| `auth_fail` | 用户名 / 密码 / 私钥认证失败 |
| `sshpass_missing` | 密码部署但**中心机**未安装 `sshpass`（请安装或改用密钥，不要从笔记本旁路） |
| `sudo_required` | 非 root 写入 `/opt/...` 时 sudo 不可用（先 `sudo -n`，否则同一 SSH 密码 `sudo -S`） |
| `key_missing` | 填写的私钥文件在中心机上不存在 |
| `no_python` | 目标机没有可用 Python ≥ 3.6 |
| `sync_tool_missing` | 同步文件失败（rsync 优先，缺失则 tar / cp 仍都不可用） |
| `package_incomplete` | 中心安装树或下发安装包缺少 `agent/`（打包从中心目录读取，**必须保留 agent 包**；rsync --delete 源不完整会删掉目标 agent） |
| `dir_not_writable` | SSH 已通但安装目录不可写（非 root 用 `~/monitor-agent`） |
| `agent_start_fail` | 上报自检或 Agent 启动失败（中心地址 / Token / 网络） |
| `center_url_missing` | 未配置「中心对外访问地址」 |
| `timeout` | 探测或整段部署超时 |
| `remote_fail` | 远端安装失败（未归入上面几类） |

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
| 部署报「未找到可用的 Python >= 3.6」 | 设置 `PYTHON_BIN` 指向本机解释器，或安装 `python3`；`/usr/local/python3.x` 会被自动探测 |
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
| GET | `/api/v1/hosts` | 主机列表（含 anomaly） |
| GET | `/api/v1/hosts/{id}` | 详情 |
| GET | `/api/v1/hosts/{id}/history` | 历史趋势（`minutes` / `limit`） |
| GET | `/api/v1/hosts/{id}/period-stats` | 单机时段统计（`minutes` 或 `from_ts`/`to_ts`；avg/min/max/p95/busy_ratio） |
| GET | `/api/v1/period-stats` | 集群时段利用（每主机 + 样本加权汇总） |
| GET | `/api/v1/stats` | 集群即时统计 |
| GET | `/api/v1/anomaly` | 异常判定汇总 |
| GET | `/api/v1/export/hosts.csv` | 导出当前主机快照 |
| GET | `/api/v1/export/period-stats.csv` | 导出当前时间窗时段报表 |
| * | `/api/v1/deploy/*` | 部署清单、设置、SSH 任务等（见部署页） |

异常阈值见中心配置中的 `anomaly` 段（示例见 `config/center.example.json`）。

## 文档

| 文档 | 说明 |
| --- | --- |
| `docs/01-requirements.md` | 需求规格 |
| `docs/02-architecture.md` | 架构设计 |
| `docs/03-design-and-test.md` | 设计与测试 |
| `docs/05-competitive-analysis.md` | 对标分析 |
| `docs/06-optimization-backlog.md` | 历史优化 backlog |
| `docs/08-cpu-npu-focus.md` | 加速卡与部署说明 |
| `docs/11-period-utilization.md` | 时段利用统计（绝对时间窗 / 集群汇总 / P95） |
| `docs/12-disk-mounts.md` | 多挂载点磁盘：过滤规则与汇总口径 |
| **`docs/10-roadmap.md`** | **下一步待办（服务器实装 / 框架化 / 拓扑图 / 容量验证）** |

## 下一步（摘要）

详见 [`docs/10-roadmap.md`](docs/10-roadmap.md)：

1. **测试部署到服务器使用**
2. **框架化**：指标变化时尽量只改采集实现
3. **拓扑 / 扩补图界面**：可视化全量链接信息
4. **技术验证**：当前实现适合同时监控多少机器

## 自测

```bash
PYTHONPATH=. python3 -m unittest tests.test_basic tests.test_v11 tests.test_v12 tests.test_excel_net tests.test_py36_compat tests.test_disk tests.test_deploy
```
