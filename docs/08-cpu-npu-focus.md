# CPU / 加速卡聚焦与自动化部署说明（v1.4）

## 需求响应

1. **不做告警通道 / 修复动作**：移除控制台告警条依赖；改为资源 **异常判定**（normal / warn / critical / unknown）。
2. **聚焦 CPU + 加速卡**：采集、统计、趋势、判定以 CPU 与加速卡为主（厂商见下）。
3. **部署自动化**：`deploy_center.sh` / `deploy_agent.sh` / 网页 SSH 部署；本地用 `local_up.sh`。

## 加速卡采集

统一入口：`agent/metrics/accelerators.py`（并兼容历史 `npus` / `gpus` 字段）。

| 厂商 | 探测方式（示例） |
| --- | --- |
| 英伟达 | `nvidia-smi` |
| 华为昇腾 | `npu-smi` |
| 寒武纪 | `cnmon` 等 |
| 瑞芯微 RKNN | rknpu 相关节点/工具 |

无对应工具时该项为空列表，不影响 CPU/内存等系统指标上报。

主机类型：

- `cpu`：只采系统，不采加速卡  
- `gpu` / `auto`：系统 + 加速卡（auto 按探测结果）

## 异常判定口径（默认可配）

| 资源 | 偏高（warn） | 异常（critical） |
| --- | --- | --- |
| CPU 利用率 | ≥80% | ≥95% |
| CPU 负载 | load1/核数 ≥ 2.0 | — |
| 加速卡 Health | Warning 等非 OK | Alarm / Critical |
| 加速卡利用率 | ≥95% | — |
| 加速卡温度 | ≥85°C | ≥95°C |
| 加速卡内存 | ≥90% | ≥98% |
| 主机离线 | — | 判定为 unknown（不臆造资源状态） |

阈值可在 `config/center.example.json` 的 `anomaly` 段调整。

## 主机详情：时间段统计

详见 [`docs/11-period-utilization.md`](11-period-utilization.md)。

- 单机：`GET /api/v1/hosts/{id}/period-stats?minutes=120`（仍可用）；亦可 `from_ts`/`to_ts`
- 集群：`GET /api/v1/period-stats?minutes=120`
- UI：监测台「时段利用」+ 详情页同步时间窗；平均 / 最低 / 最高 / P95 / 繁忙占比；可导出 CSV

## 自动化部署行为

### 本地试用 `local_up.sh` / `local_down.sh`

- 推荐本机试用入口；使用 `scripts/daemonize_run.py` **双重 fork** 守护化  
- 避免在 IDE/SSH 会话里 `python -m center &`：会话结束进程常被连带杀掉  

### 服务端 `deploy_center.sh`

1. 同步代码到安装目录  
2. 若不存在则生成 `center.json`（含 anomaly 阈值）  
3. root + systemd → 安装 `monitor-center.service` 并 enable  
4. 非 root / `--no-systemd` → 守护进程后台启动（`daemonize_run.py`）  
5. 轮询 `/api/v1/health` 直至成功  

### 客户端与网页部署

- `deploy_agent.sh`：本机变量 `INSTALL_DIR`；非 root 默认 `~/monitor`  
  - **Python**：先复用已有解释器（`PYTHON_BIN` → PATH 中 `python3.15`…`python3.6` → `/usr/local/python3.*` → `python3`），要求 **≥ 3.6**；都没有且为 root 时再 apt/yum/dnf 安装 `python3`  
  - `/usr/local/python3.x` 若 libpython 不在默认链接路径，自动设置 `LD_LIBRARY_PATH`  
  - **同步**：优先 `rsync`；目标机/本机没有则静默回退 `tar` 管道或 `cp -a`（`scripts/lib/sync_tree.sh`），不捆绑离线 Python  
- SSH 在线部署：远端使用 `REMOTE_DIR`（勿再嵌套未赋值的 `INSTALL_DIR`）  
  - 清单/Excel/`deploy_fleet.sh` 支持可选 **ssh_port**（默认 22；CLI `--ssh-port` / `--port`）；**不扫描端口**  
  - 连接超时 / connection closed 自动重试 2～3 次（1s、2s 退避）  
  - 失败归一为短码 + 说明（`ssh_unreachable`、`auth_fail`、`no_python`、`sync_tool_missing`、`agent_start_fail` 等，见 README）  
- 添加/导入目标或保存中心对外地址时 **自动探查** 已配置地址  
- 保留「测试连通/部署条件」（SSH + 目录可写），已去掉独立「探测勾选」按钮  

## 下一步

见 `docs/10-roadmap.md`（服务器实装、指标框架化、拓扑图、容量验证）。
