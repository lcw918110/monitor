# 中心配置旁注

`config/center.example.json` 是一份填了默认值的样例。复制成 `config/center.json` 后，没写的键用代码里的同一套默认。设置页 `/settings.html` 按下面的分组编辑，说明和这里一致。

保存到设置页后，除监听地址、端口、数据目录外，当前进程立即使用新值。手改文件要重启 `monitor-center`。这不是升级。管理 Token 和 Webhook 不在这个文件里。

改 `period_util` 不会改诊断报告的空闲线和高负荷线。那两条在 `report`。

## 保留与清理

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `retention_days` | 7 | 历史上报保留天数。只删 `metrics_history`，不删已生成的诊断报告。0 表示不按天数删除，查询窗也不再裁成 7 天。 |
| `offline_seconds` | 90 | 多少秒没上报算离线。影响监测台显示，以及离线主机不计入基本不用。 |
| `cleanup_interval_seconds` | 3600 | 两次清理的间隔（秒），至少 60。不改变保留天数。 |

## 异常与负载着色

都在 `anomaly`。只改黄/红和今日关注，不新增指标。

| 键 | 默认 | 单位 |
| --- | --- | --- |
| `cpu_load_per_core_warn` | 2.0 | load1、load5 除以核数。达到就偏高。load15 不参与。 |
| `cpu_warn_percent` / `cpu_critical_percent` | 80 / 95 | %。偏高线必须低于异常线。 |
| `mem_warn_percent` / `mem_critical_percent` | 85 / 95 | %。报告里的「内存偏高」用偏高线。 |
| `disk_warn_percent` / `disk_critical_percent` | 85 / 95 | %。报告磁盘分档用这两条。 |
| `accel_util_warn_percent` | 95 | %。只有偏高档。 |
| `accel_temp_warn_c` / `accel_temp_critical_c` | 85 / 95 | ℃。 |
| `accel_mem_warn_percent` / `accel_mem_critical_percent` | 90 / 98 | %。 |

旧键 `npu_util_warn_percent`、`npu_temp_warn_c`、`npu_temp_critical_c`、`npu_mem_warn_percent`、`npu_mem_critical_percent` 仍认。同一项没写对应的 `accel_*` 时，用旧键。两边都写时以 `accel_*` 为准。设置页恢复默认会把两个键一起收回内置值。

## 诊断报告阈值

都在 `report`。未配置时用下表。已入库的报告不自动重算。

| 键 | 默认 | 单位 | 含义 |
| --- | --- | --- | --- |
| `idle_avg_lt` | 5 | % | 基本不用：平均利用率必须低于它 |
| `idle_busy_lt` | 0.05 | 比例（0.05 = 5%） | 基本不用：繁忙占比必须低于它 |
| `high_busy_gte` | 0.40 | 比例（0.40 = 40%） | 高负荷：繁忙占比达到即算 |
| `high_p95_gte` | 85 | % | 高负荷：P95 达到也算 |
| `min_samples` | 10 | 条 | 少于此数不判闲忙，写样本不足 |

CPU 和加速卡共用这几条。有加速卡时，基本不用还要求卡的平均和繁忙也过线。高负荷与基本不用冲突时算高负荷。

## 时段利用率

`period_util.busy_cpu_percent`、`busy_accel_percent` 默认 80。一条样本大于等于它就计入繁忙占比。`-1` 表示该指标不算繁忙占比。

`busy_mem_percent`、`busy_disk_percent` 默认不写，表示不计算。内存和磁盘不进入诊断报告的闲忙两极。查询参数仍可临时覆盖。

## 报告生成时刻

`report_schedule` 可以整段省略。默认总开关开着：日报每天 00:10，周报每周一 00:20，月报每月 1 日 00:30（上海时间）。`hour` 为 0–23，`minute` 为 0–59。周几和月报的日期不能改。错过的那一分钟不补。

## 推送与安全

`token` 是 Agent 上报口令，默认空（上报不校验）。只用于 `POST /api/v1/metrics`。设置页不回显明文。它不是管理 Token。

管理 Token 和 Webhook 在数据库 `admin_settings`，见 `docs/13-diagnostic-reports.md`。

## 服务与部署

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `host` | `0.0.0.0` | 监听地址。重启后才换。 |
| `port` | 8080 | 监听端口。重启后才换。 |
| `data_dir` | `data` | 数据库目录。重启后使用新目录，不会搬走现有库。 |
| `deploy.python_bundles` | 空则自动选择 | 便携 Python 目录。环境变量 `MONITOR_PYTHON_BUNDLES` 优先。 |

样例里的 `deploy.python_bundles` 写成了 `/opt/monitor/vendor/python`。不写这一键时，有该目录就用它，否则用仓库里的 `vendor/python`。
