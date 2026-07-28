# 软件工程过程说明

本项目按标准软件工程阶段推进，产物与目录对应如下。

## 阶段与产物

| 阶段 | 产物 | 路径 |
| --- | --- | --- |
| 需求分析 | SRS（对标市场裁剪范围） | `docs/01-requirements.md` |
| 概要设计 | 架构、模块、数据流 | `docs/02-architecture.md` |
| 详细设计 / 测试设计 | API、页面、用例 | `docs/03-design-and-test.md` |
| 实现 | 中心端 / 采集端 / Web | `center/` `agent/` |
| 构建与部署 | 脚本与配置样例 | `scripts/` `config/` |
| 验证 | 单元测试与联调 | `tests/` |

## 技术选型决策记录

1. **不引入外部监控产品**：避免 Prometheus/Grafana/Zabbix 运维栈，满足「基本语句自研」约束。
2. **Push 模型**：Agent 主动上报，便于跨网段统一汇聚。
3. **SQLite + 标准库 HTTP**：单机中心端零第三方依赖，便于统一分发。
4. **GPU 通过 nvidia-smi**：复用驱动自带工具，不 bundling DCGM。

## 后续迭代建议（非本期）

- 告警规则与通知通道
- 简易历史折线图（读 `metrics_history`）
- systemd / 开机自启单元文件
- HTTPS 与更完善的鉴权
