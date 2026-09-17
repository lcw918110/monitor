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
- **部署**：Excel/CSV 批量导入；SSH（用户名密码或私钥）；配置地址自动网络探查；一键脚本

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

## 一键部署

### 1. 服务端（中心机）

```bash
chmod +x scripts/*.sh
./scripts/deploy_center.sh --port 8080
# 可选: --token 'your-secret' --dir /opt/monitor --no-systemd
```

成功后打开：`http://<中心IP>:8080/`  
客户端部署页：`http://<中心IP>:8080/deploy.html`

非 root 时默认装到 `~/monitor`，并以守护进程后台运行（不依赖当前终端）。

### 网页自动部署（推荐）

1. 打开 **客户端部署** 页  
2. 「中心对外访问地址」**默认自动填本机局域网 IP**（如 `http://192.168.x.x:8080`），供远端 Agent 上报；**一般不要用 127.0.0.1**  
3. **SSH**：支持用户名密码或私钥。填法：用户 + 密码；或用户 + 私钥。端口默认 **22**  
   - 全局「私钥路径」= 多台客户端**共用**一把钥匙（不是按 IP 一对一）  
   - 某台要用不同密码/私钥：在添加该客户端（或 Excel 的 `ssh_password` / `ssh_key_path` 列）单独填  
4. 中心机需能 SSH 到目标机（密码或密钥任一通即可）  
5. 添加 / 编辑 / 导入目标机时，会对已配置的 IP、指定机器、中心对外地址 **自动探查**；清单「网络」列展示结果  
6. 部署前可用「测试连通/部署条件」（SSH + 安装目录可写）；再点「部署勾选 / 全部部署」  

安装目录说明：

- 本机安装脚本内部变量为 `INSTALL_DIR`（`deploy_agent.sh` / `deploy_center.sh`）
- **SSH 在线部署**路径使用 `REMOTE_DIR`（支持 `~/monitor`）；非 root 勿填 `/opt/monitor`
- 也可下载「通用安装脚本」在目标机手动执行

### 2. 客户端（每台被监控机）

```bash
./scripts/deploy_agent.sh --center-url http://<中心IP>:8080
# 可选: --host-id gpu01 --token 'your-secret' --interval 15 --dir ~/monitor
```

脚本会：解析 Python（先复用本机 `PYTHON_BIN` / `python3.x` / `/usr/local/python3.*`，均需 ≥3.6；都没有且为 root 时再 apt/yum/dnf 安装 python3）→ 同步文件 → 写配置 → 探测加速卡工具 → 单次上报自检 → systemd 或守护进程常驻。

内网常见情况：只有 3.6、PATH 里的 `python3` 过旧但 `/usr/local/python3.12` 可用、或完全没有 python3——脚本按「先复用、再安装」处理。自定义前缀若缺 libpython，会自动加上 `LD_LIBRARY_PATH`。

### 3. 批量部署多台 Agent（需 SSH）

```bash
cp config/hosts.example.txt config/hosts.txt
# 编辑 IP / host_id
./scripts/deploy_fleet.sh --hosts-file config/hosts.txt --center-url http://<中心IP>:8080
```

### 4. 卸载

```bash
./scripts/uninstall.sh --role agent
./scripts/uninstall.sh --role center --purge-data
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
| 部署页无法 SSH 安装 | 检查用户名/密码或私钥；全局私钥多机共用，单机可单独覆盖；端口默认 22；非 root 安装目录用 `~/monitor` |
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
PYTHONPATH=. python3 -m unittest tests.test_basic tests.test_v11 tests.test_v12 tests.test_excel_net tests.test_py36_compat tests.test_disk
```
