# 简易多机监控系统 — 软件需求规格说明书（SRS）

| 文档版本 | 1.0 |
| 编制日期 | 2026-07-27 |
| 状态 | 已确认开发基线 |

---

## 1. 引言

### 1.1 目的

本文档定义「简易多机监控系统」（以下简称本系统）的功能需求、非功能需求与约束，作为设计、实现、测试与验收的依据。

### 1.2 背景与问题陈述

机房/实验室中存在多台 **GPU 服务器** 与 **应用服务器**，需要统一掌握主机存活、算力占用与基础资源使用情况。市场同类产品（Prometheus + Grafana、Zabbix、Nightingale、Datadog Agent、DCGM-Exporter 等）能力完善，但引入链路长、依赖重、运维成本高。

本系统目标：**不引入外部监控类软件产品**，仅使用操作系统自带能力与通用编程语言标准库，实现「中心统一部署 + Agent 采集回传 + Web 展示统计」。

### 1.3 同类软件对标（能力裁剪）

| 能力维度 | 市场常见做法 | 本系统范围 |
| --- | --- | --- |
| 采集模型 | Pull（Exporter）或 Push（Agent） | **Push**：Agent 主动上报 |
| 主机指标 | node_exporter / Zabbix agent | `/proc`、命令行只读解析 |
| GPU 指标 | DCGM / nvidia-smi exporter | `nvidia-smi` 查询（有则采，无则跳过） |
| 存储 | TSDB / MySQL | 中心端 **SQLite**（单机易部署） |
| 展示 | Grafana 面板 | 自研静态 Web 页 + JSON API |
| 告警 | 规则引擎、通知通道 | **本期不做**（预留扩展） |
| 鉴权 | LDAP / SSO | 简易 Token（可选） |

### 1.4 术语

| 术语 | 说明 |
| --- | --- |
| 中心端（Center） | 统一部署的服务：接收指标、持久化、提供 API 与页面 |
| 采集端（Agent） | 部署在被监控主机上的轻量进程，周期采集并回传 |
| 主机（Host） | 被监控的一台物理机或虚拟机 |
| 指标（Metric） | 某一时刻的数值型或结构化监控数据 |
| GPU 服务器 | 安装 NVIDIA 驱动、可通过 `nvidia-smi` 查询的主机 |
| 应用服务器 | 以 CPU/内存/磁盘/网络为主的业务主机 |

### 1.5 约束

1. **不引入** Prometheus、Grafana、Zabbix、InfluxDB、Elastic、DCGM 等外部监控软件作为运行依赖。
2. 实现以 **Python 3 标准库** 为主（`http.server`、`sqlite3`、`urllib`、`subprocess`、`json` 等）。
3. GPU 采集依赖主机已安装的 `nvidia-smi`（驱动自带工具），不 bundling 第三方 GPU 库。
4. 中心端支持单机一键式部署；Agent 配置中心地址后即可工作。

---

## 2. 总体描述

### 2.1 产品愿景

运维/研发人员在浏览器打开中心端页面，即可查看全部接入主机的在线状态、基础资源与 GPU 使用概况，并对集群做汇总统计。

### 2.2 用户角色

| 角色 | 职责 |
| --- | --- |
| 系统管理员 | 部署中心端、分发 Agent、配置 Token |
| 运维/研发查看者 | 浏览主机列表、详情与统计页 |

### 2.3 运行环境假设

- 操作系统：Linux（主）/ macOS（开发验证）
- Python：3.8+
- 网络：Agent 可访问中心端 HTTP 端口
- GPU 主机：可选 NVIDIA 驱动与 `nvidia-smi`

### 2.4 部署拓扑

```
  [GPU Agent] ──┐
  [GPU Agent] ──┼── HTTP POST /api/v1/metrics ──▶ [Center]
  [App Agent] ──┘                                      │
                                                       ├─ SQLite
                                                       └─ Web UI /
```

---

## 3. 功能需求

### 3.1 中心端统一部署（FR-01）

| ID | 需求描述 | 优先级 |
| --- | --- | --- |
| FR-01-01 | 提供中心端启动入口与示例配置，单机即可运行 | P0 |
| FR-01-02 | 中心端监听可配置的 host/port | P0 |
| FR-01-03 | 数据目录可配置，默认落盘 SQLite | P0 |
| FR-01-04 | 提供安装/启动脚本，便于统一分发 | P1 |

### 3.2 指标采集与回传（FR-02）

| ID | 需求描述 | 优先级 |
| --- | --- | --- |
| FR-02-01 | Agent 周期性采集本机指标并 POST 到中心端 | P0 |
| FR-02-02 | 上报负载含：主机标识、主机类型、时间戳、指标体 | P0 |
| FR-02-03 | 应用/通用指标：CPU 使用率、内存总量/已用、磁盘用量、负载、开机时长 | P0 |
| FR-02-04 | GPU 指标（若存在）：卡数、每卡利用率、显存、温度、功耗、名称 | P0 |
| FR-02-05 | 无 GPU 时 Agent 仍可正常上报主机指标 | P0 |
| FR-02-06 | 支持配置采集间隔、中心 URL、主机名、Token | P0 |
| FR-02-07 | 上报失败应记录日志并可在下次周期重试（不丢进程） | P1 |

### 3.3 资源展示与统计（FR-03）

| ID | 需求描述 | 优先级 |
| --- | --- | --- |
| FR-03-01 | 提供 Web 首页：主机列表（名称、类型、在线状态、最近上报时间） | P0 |
| FR-03-02 | 主机详情：最新基础指标与 GPU 明细 | P0 |
| FR-03-03 | 统计区：主机总数、在线数、GPU 主机数、GPU 卡总数、集群平均 CPU/内存、GPU 平均利用率 | P0 |
| FR-03-04 | 页面自动刷新（可配置间隔，默认 15s） | P1 |
| FR-03-05 | 提供只读 JSON API 供页面调用 | P0 |
| FR-UTIL-PERIOD | 按时间段统计单机与集群利用率（相对 `minutes` + 绝对 `from_ts`/`to_ts`；avg/min/max/p95/busy_ratio；CSV） | P1 |

### 3.4 主机识别与在线判定（FR-04）

| ID | 需求描述 | 优先级 |
| --- | --- | --- |
| FR-04-01 | 以 `host_id`（可配置，默认 hostname）唯一标识主机 | P0 |
| FR-04-02 | 超过阈值（默认 90s）未上报判定为离线 | P0 |
| FR-04-03 | 主机类型：`gpu` / `app` / `auto`（auto 根据是否采到 GPU 判定） | P0 |

---

## 4. 非功能需求

| ID | 类别 | 描述 |
| --- | --- | --- |
| NFR-01 | 性能 | 中心端支持 ≥50 Agent、默认 15s 上报间隔下稳定写入 |
| NFR-02 | 可靠性 | 中心进程重启后历史最新快照可恢复；明细保留可配置天数（默认 7 天） |
| NFR-03 | 可维护性 | 代码结构清晰：center / agent / docs / scripts 分离 |
| NFR-04 | 安全性 | 可选共享 Token；生产建议内网部署 |
| NFR-05 | 可移植性 | 不依赖第三方 pip 包即可运行（标准库） |
| NFR-06 | 易用性 | 中文界面，关键数字一眼可读 |

---

## 5. 数据需求（摘要）

### 5.1 上报报文（JSON）

```json
{
  "host_id": "gpu-01",
  "hostname": "gpu-01.lab",
  "host_type": "gpu",
  "timestamp": 1722096000,
  "token": "optional-secret",
  "system": {
    "cpu_percent": 23.5,
    "mem_total_mb": 128000,
    "mem_used_mb": 64000,
    "mem_percent": 50.0,
    "disk_total_gb": 1800,
    "disk_used_gb": 900,
    "disk_percent": 50.0,
    "load1": 2.1,
    "uptime_sec": 864000
  },
  "gpus": [
    {
      "index": 0,
      "name": "NVIDIA A100-SXM4-40GB",
      "util_percent": 80,
      "mem_total_mb": 40960,
      "mem_used_mb": 20000,
      "temp_c": 62,
      "power_w": 250
    }
  ]
}
```

### 5.2 存储要点

- `hosts`：主机元数据与最新快照
- `metrics`：历史上报（用于简单趋势，可选清理）

---

## 6. 接口需求（概要）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/metrics` | Agent 上报 |
| GET | `/api/v1/hosts` | 主机列表 |
| GET | `/api/v1/hosts/{host_id}` | 主机详情 |
| GET | `/api/v1/stats` | 集群统计 |
| GET | `/` | Web 控制台 |

---

## 7. 本期不做（Out of Scope）

- 告警通知、邮件/企微推送
- 分布式高可用中心端、多租户
- 容器/K8s 深度监控、应用 APM
- 长期时序分析与复杂图表引擎
- 自动发现与无 Agent 监控

---

## 8. 验收标准

1. 单机启动中心端后，浏览器可打开控制台。
2. 至少 1 台应用主机、1 台（或模拟）GPU 主机完成 Agent 上报。
3. 列表页显示在线状态；详情页显示 CPU/内存/磁盘及 GPU 字段（有则显示）。
4. 统计页数字与列表汇总一致。
5. 停止 Agent 超过离线阈值后，状态变为离线。
6. 全程无 Prometheus/Grafana/Zabbix 等外部监控软件依赖。

---

## 9. 需求追踪矩阵（开发基线）

| 需求 ID | 设计模块 | 测试用例 |
| --- | --- | --- |
| FR-01 | center + scripts | TC-DEP-01 |
| FR-02 | agent | TC-AGT-01~04 |
| FR-03 | center/static + API | TC-UI-01~03 |
| FR-04 | center storage 判定 | TC-ONLINE-01 |
