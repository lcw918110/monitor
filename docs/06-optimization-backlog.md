# 自主优化 backlog

状态约定：`todo` / `doing` / `done` / `blocked-human`

## 已完成（v1.x）

| ID | 项 | 状态 | 说明 |
| --- | --- | --- | --- |
| O1 | 同类对比文档 | done | docs/05 |
| O2 | 历史序列 API | done | `/api/v1/hosts/{id}/history` |
| O3 | 详情页趋势图 | done | SVG sparkline |
| O4 | 阈值异常判定 | done | CPU/内存/磁盘/加速卡温度等（页面着色，非通知通道） |
| O5 | 告警列表 API + 页头告警条 | done | 后续改为 anomaly；通知通道见 H1 |
| O6 | 网络收发速率采集 | done | `/proc/net/dev` |
| O7 | GPU 计算进程采集 | done | nvidia-smi compute-apps |
| O8 | systemd / 守护进程 | done | `deploy_*.sh` + `daemonize_run.py` + `local_up.sh` |
| O9 | 主机 CSV/JSON 导出 | done | `/api/v1/export/*` |
| O10 | 演示数据种子脚本 | done | `scripts/demo_seed.py` |
| O11 | 测试覆盖扩展 | done | `tests/test_v11.py` 等 |
| O12 | 阶段结论报告 | done | docs/07-morning-report.md |
| O13 | 多厂商加速卡 | done | 英伟达 / AMD / 昇腾 / 寒武纪 / RKNN |
| O14 | 网页 SSH 部署与 Excel 导入 | done | deploy 页；密码/私钥；自动探查 |
| O15 | 主机详情时间段统计 | done | `/period-stats` + 1h～7d 切换 |
| O16 | 时段利用（绝对窗/集群/P95） | done | `docs/11-period-utilization.md` |
| O17 | 日/周/月使用诊断报告 | done | `docs/13-diagnostic-reports.md`；空闲阈值 5% |
| O18 | 诊断报告内置调度 | done | 错过不补；同一窗口覆盖；`generated_at` 与越界样本排除 |

## 需人工决策（历史）

| ID | 项 | 状态 | 说明 |
| --- | --- | --- | --- |
| H1 | 邮件/企微告警通道 | blocked-human | 需 Webhook/SMTP |
| H2 | 生产 Token 与绑定网卡 | blocked-human | 需环境信息 |
| H3 | DCGM 级深度指标 | blocked-human | 与「不引入外部监控」冲突，需决策 |
| H4 | 中心高可用 | 已拍板 | 做备份/恢复（弱高可用，见 `docs/14`）和容量摸底方法（见 `docs/15`，数字待实测）。双中心 / 双活不做 |

## 下一步

完整说明见 **`docs/10-roadmap.md`**。

| ID | 项 | 状态 | 说明 |
| --- | --- | --- | --- |
| N1 | 服务器实装测试 | todo | 中心+Agent 真实环境闭环 |
| N2 | 指标框架化 | todo | 加指标优先只改采集实现 |
| N3 | 拓扑/扩补图界面 | todo | 可视化全量链接与探查关系 |
| N4 | 容量技术验证 | todo | 探针和空白结果表已有（`docs/15-capacity-lab.md`）。主机上限等实测填写，不在文档里猜 |

**当前焦点**：U1 已交付顶栏、今日关注、白话健康和报告深链，长期空闲名单仍待。下一优先是 **U2**（U1 收尾 → U2 → U3，见路线图）。长期运维是 O1–O7。实装与能力项仍按 N1 → N4 → N2 → N3。2026-10-27 的债务和待人拍板项见 `docs/audit-tech-debt-2026-10-27.md`，不要把本文的 done 行重新打开。
