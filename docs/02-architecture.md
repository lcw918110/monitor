# 简易多机监控系统 — 架构设计说明书

| 文档版本 | 1.1 |
| 对应 SRS | docs/01-requirements.md |
| 路线图 | docs/10-roadmap.md |

---

## 1. 设计原则

1. **自研轻量**：不依赖外部监控产品；Python 3 标准库实现。
2. **中心汇聚**：所有展示与统计只读中心端数据。
3. **推送模型**：Agent 主动上报，便于穿透 NAT/防火墙（出站即可）。
4. **加速卡可选**：多厂商探测，失败不影响主机指标。
5. **单库单进程**：中心端单进程 HTTP + SQLite，降低统一部署成本。

---

## 2. 逻辑架构

```
┌──────────────────────────────────────────────────────────┐
│                     浏览器 (运维人员)                      │
│         GET /  ·  /deploy.html  ·  /api/v1/*              │
└────────────────────────────┬─────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────┐
│                     Center（中心端）                        │
│  HTTP Router → API / Deploy API → Storage (SQLite)       │
│  Static Web：监测台 · 主机详情(时段统计) · 部署页           │
└────────────────────────────▲─────────────────────────────┘
                             │ POST /api/v1/metrics
        ┌────────────────────┼────────────────────┐
        │                    │                    │
┌───────┴──────┐     ┌───────┴──────┐     ┌───────┴──────┐
│ Agent (gpu)  │     │ Agent (auto) │     │ Agent (cpu)  │
│ system+accel │     │ 探测后采集    │     │ system only  │
└──────────────┘     └──────────────┘     └──────────────┘
```

---

## 3. 模块划分

| 模块 | 路径 | 职责 |
| --- | --- | --- |
| 中心服务入口 | `center/server.py` | HTTP 服务、路由、静态资源 |
| 存储层 | `center/storage.py` | SQLite：主机快照、历史、时段聚合、清理 |
| 监测 API | `center/api.py` | 上报校验、列表/详情/history/period-stats/集群时段利用 |
| 异常判定 | `center/anomaly.py` | 阈值着色判定 |
| 部署 | `center/deploy_*.py` | 清单、SSH 部署、探查、Excel |
| Web UI | `center/static/*` | 监测台、部署页 |
| Agent 入口 | `agent/main.py` | 配置加载、循环调度 |
| 系统采集 | `agent/metrics/system.py` | CPU/内存/磁盘/负载/uptime/网络等 |
| 加速卡采集 | `agent/metrics/accelerators.py` | 多厂商加速卡 |
| 上报 | `agent/sender.py` | HTTP POST |
| 配置样例 | `config/*.example.json` | 中心/Agent 配置 |
| 脚本 | `scripts/*` | 安装、本地守护、批量部署；`scripts/lib/resolve_python.sh` 复用本机 Python |

---

## 4. 数据与容量（概要）

- **hosts**：每机最新完整 payload  
- **metrics_history**：压缩历史点；默认保留约 7 天（`retention_days`）  
- **部署库**：与监测库可同机；目标清单、任务日志、探查结果  

容量与并发适合规模见路线图 **N4**（待压测验证）。框架化扩展指标见 **N2**。

---

## 5. 相关文档

- 需求：`docs/01-requirements.md`
- 设计与测试：`docs/03-design-and-test.md`
- 部署与加速卡：`docs/08-cpu-npu-focus.md`
- 下一步：`docs/10-roadmap.md`
