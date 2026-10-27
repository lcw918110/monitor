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
│  Static Web：实时监控 / 时段统计 / 使用诊断 · 部署页        │
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
| 诊断报告 | `center/reports.py` | 日/周/月窗口、闲忙口径、Markdown；结果表 `diagnostic_reports` |
| 报告调度 | `center/report_schedule.py` | 进程内定时生成；错过不补；成功后 `deliver_report` |
| 展示地址 | `center/hostaddr.py` | 列表/详情/时段统计/诊断报告的完整 IP：手工 `address` → 未停用的部署清单 → host_id 内嵌 IP → RFC1918；跳过 `198.18/15` 隧道假 IP。历史 IP 在 `asset_meta.previous_ips` |
| 异常判定 | `center/anomaly.py` | 阈值着色判定 |
| 部署 | `center/deploy_*.py` | 清单、SSH 部署、探查、Excel |
| 管理设置 | `center/admin_settings.py` | 可选管理 Token（只存 SHA-256）与报告推送配置，表 `admin_settings` |
| 报告推送 | `center/report_push.py` | 企业微信 / 钉钉 / 飞书 Webhook；未配置不发请求 |
| Web UI | `center/static/*` | 监测台（实时监控 / 时段统计 / 使用诊断）、部署页 `/deploy.html`、设置页 `/settings.html` |
| Agent 入口 | `agent/main.py` | 配置加载、循环调度 |
| 系统采集 | `agent/metrics/system.py` | CPU/内存/负载/uptime/网络；磁盘见 `disk.py` |
| 磁盘采集 | `agent/metrics/disk.py` | 多挂载点过滤 + 汇总 `disk_*` / `disks[]`（口径见 `docs/12-disk-mounts.md`） |
| 加速卡采集 | `agent/metrics/accelerators.py` | 多厂商加速卡（NVIDIA / AMD / 昇腾 / 寒武纪 / RKNN） |
| AMD GPU 采集 | `agent/metrics/amd.py` | `rocm-smi` 优先，`amd-smi` 回退 |
| 上报 | `agent/sender.py` | HTTP POST |
| 配置样例 | `config/*.example.json` | 中心/Agent 配置 |
| 脚本 | `scripts/*` | 唯一部署路径：`deploy_center.sh`、网页 SSH、`deploy_fleet.sh` / `deploy_agent.sh`；`install_agent.sh` 仅为别名。中心升级只走 `push_upgrade.sh` → 新包里的 `upgrade_center.sh`。`scripts/lib/resolve_python.sh` 与 `python_min.sh` 复用本机 Python（Agent 3.6 / 中心 3.8）；`python_bundle.sh` 解压便携解释器；`scripts/lib/sync_tree.sh` 同步回退并保留 `vendor/python/*.tar.gz`；`scripts/lib/unit_env.sh` 重写 systemd 单元时保留自定义 `Environment=`；`scripts/lib/deploy_ssh.sh` 端口/重试 |

---

## 4. 数据与容量（概要）

- **hosts**：每机最新完整 payload。可空列 `hidden` / `hidden_at` / `asset_meta`（手工资产 JSON）。NULL 视为未隐藏。迁移只加列，不重写 `metrics_history`
- **metrics_history**：压缩历史点，保留天数由 `retention_days` 决定（示例 7，可按环境加长）。有 `disks[]` 时另存精简挂载，供诊断报告点名最满盘
- **diagnostic_reports**：已生成的日/周/月诊断报告（Markdown + JSON，含 `source` / `status` / `generated_at`）。读取不重算。`retention_days` 不删除这张表
- **resource_groups** / **host_group_members**：中心侧资源组
- **部署库**：与监测库可同机；目标清单、任务日志、探查结果。Agent 安装只走产品路径（`/deploy.html`、`deploy_fleet.sh`），不包含笔记本 scp 旁路
- **admin_settings**：管理 Token 哈希、报告推送开关 / 渠道 / Webhook。与监测库同一个 SQLite。接口不回显 Token 明文和完整 Webhook  

容量与并发适合规模见路线图 **N4**（待压测验证）。框架化扩展指标见 **N2**。

---

## 5. 相关文档

- 需求：`docs/01-requirements.md`
- 设计与测试：`docs/03-design-and-test.md`
- 部署与加速卡：`docs/08-cpu-npu-focus.md`
- 多挂载磁盘：`docs/12-disk-mounts.md`
- 使用诊断报告：`docs/13-diagnostic-reports.md`
- 下一步：`docs/10-roadmap.md`
