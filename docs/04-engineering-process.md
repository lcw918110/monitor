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
4. **加速卡走厂商自带工具**：英伟达 `nvidia-smi`、AMD `rocm-smi`/`amd-smi`、昇腾、寒武纪、RKNN。不 bundling DCGM。
5. **Agent 只走产品路径**：`/deploy.html` 与中心机 `deploy_*.sh`。笔记本 sshpass/scp 旁路不作为安装方式。

## 已经落地、不要再当成待办

历史折线、时段统计、systemd/守护进程、资源组、多盘、AMD 与 `accel_summary`、离线不展示实时数字、日/周/月诊断报告入库，都已在当前版本。

## 后续仍未做

- 告警通知通道（页面只做阈值着色）
- 诊断报告的自动定时生成（v1 为手动）
- HTTPS 与比 Agent Token 更完整的控制台鉴权
