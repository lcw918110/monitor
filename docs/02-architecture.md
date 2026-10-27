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
| 报告调度 | `center/report_schedule.py` | 进程内定时生成；错过不补；成功后 `deliver_report`（已配置 Webhook 才发送） |
| 展示地址 | `center/hostaddr.py` | 列表/详情/时段统计/诊断报告的完整 IP：手工 `address` → 未停用的部署清单 → host_id 内嵌 IP → RFC1918；跳过 `198.18/15` 隧道假 IP。历史 IP 在 `asset_meta.previous_ips` |
| 手工资产 / 隐藏 | `center/hostmeta.py` | 负责人、用途、资产字段、隐藏与导入。上报不改写手工文字 |
| 同机改址 | `center/readdress.py` | 更换展示 IP，旧地址进历史；可撤销 |
| 白话状态 | `center/status_text.py` | 加速卡健康的中文说法，规则与 `center/static/ux.js` 一致 |
| 异常判定 | `center/anomaly.py` | 阈值着色判定。`npu_*` 键是旧配置别名 |
| 便携解释器 | `center/python_bundle.py` | 按架构选择中心上的 CPython 包 |
| 部署 | `center/deploy_*.py` | 清单、SSH 部署、探查、Excel |
| 管理设置 | `center/admin_settings.py` | 可选管理 Token（只存 SHA-256）与报告推送配置，表 `admin_settings` |
| 报告推送 | `center/report_push.py` | 企业微信 / 钉钉 / 飞书 Webhook；未配置不发请求 |
| Web UI | `center/static/*` | 监测台（实时监控 / 时段统计 / 使用诊断）、部署页 `/deploy.html`、设置页 `/settings.html` |
| Agent 入口 | `agent/main.py` | 配置加载、循环调度 |
| 系统采集 | `agent/metrics/system.py` | CPU/内存/负载/uptime/网络；磁盘见 `disk.py` |
| 磁盘采集 | `agent/metrics/disk.py` | 多挂载点过滤 + 汇总 `disk_*` / `disks[]`（口径见 `docs/12-disk-mounts.md`） |
| 加速卡采集 | `agent/metrics/accelerators.py` | 编排 NVIDIA / AMD / 昇腾 / 寒武纪 / RKNN；没有厂商工具时从 PCI 识别已知卡 |
| 厂商采集 | `agent/metrics/gpu.py`、`amd.py`、`npu.py` | 英伟达、AMD、昇腾与寒武纪。由 `accelerators.py` 调用，不是第二条入口 |
| 命令与 PCI | `agent/metrics/probe.py` | 跑厂商工具、合并 PCI、整理单卡字段 |
| 上报 | `agent/sender.py` | HTTP POST |
| 配置样例 | `config/*.example.json` | 中心/Agent 配置 |
| 脚本 | `scripts/*` | 首次装中心：`deploy_center.sh`。Agent：网页 SSH 或中心机 `deploy_fleet.sh` / `deploy_agent.sh`；`install_agent.sh` 仅为别名。已安装中心的升级只走 `push_upgrade.sh` → 新包里的 `upgrade_center.sh`（固定 `/opt/monitor`）。`scripts/lib/sync_tree.sh` 是同步函数库，不能直接执行。`resolve_python.sh` 与 `python_min.sh` 复用本机 Python（Agent 3.6 / 中心 3.8）；`python_bundle.sh` 解压便携解释器；`unit_env.sh` 重写单元时保留自定义 `Environment=`；`deploy_ssh.sh` 端口与重试。操作步骤见 `docs/14-ops-runbook.md` |

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
- 升级、便携 Python、隐藏主机：`docs/14-ops-runbook.md`
