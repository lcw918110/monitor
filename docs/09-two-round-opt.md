# 自主优化两轮记录（v1.2.1）

## 第 1 轮：采集 / 判定 / 存储稳健性

| 项 | 说明 |
| --- | --- |
| 历史上报压缩 | `metrics_history` 只存趋势字段，hosts 仍保留完整快照 |
| SQLite WAL | 提升并发读写稳定性 |
| Agent 上报重试 | 网络抖动时最多重试 2 次（退避） |
| 配置校验 | 启动前校验 center/agent 关键字段 |
| CPU 负载 | 采集 load1/5/15；判定增加 load5 |
| NPU 补全 | `info` 缺字段时尝试 `npu-smi info -t usages` |
| 清理 | 移除已废弃 `alerts.py` |

## 第 2 轮：部署自动化与控制台可用性

| 项 | 说明 |
| --- | --- |
| 批量部署 | `scripts/deploy_fleet.sh` + `config/hosts.example.txt`（SSH 并行） |
| 卸载脚本 | `scripts/uninstall.sh --role center\|agent` |
| 部署排除 | rsync 排除 `.deploy-*` / `*.db`，避免嵌套污染 |
| 主机排序 | 在线优先，异常等级 critical→warn→unknown→normal |
| 控制台筛选 | 搜索、类型、判定状态下拉过滤 |
| 版本 | 升至 **1.2.1** |

## 验证

```bash
PYTHONPATH=. python3 tests/test_basic.py
PYTHONPATH=. python3 tests/test_v11.py
PYTHONPATH=. python3 tests/test_v12.py
```

两轮完成后停止自主优化；后续若要上通知通道或 DCGM，需人工决策。
