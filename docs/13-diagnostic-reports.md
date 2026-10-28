# 使用诊断报告（日 / 周 / 月）

中心在本地把窗口内的原始历史上报聚合成中文诊断报告，写入 SQLite。页面和 API 读库存结果，不把已经生成的日报/周报拼成月报。

监测台页签：**使用诊断**（`/#reports`）。

调度在中心进程里跑，不依赖外部 cron，也不依赖外面的助手来调接口。

## 时间窗

时区 **Asia/Shanghai**（无夏令时，UTC+8）。区间是半开的：含起点，不含结束时刻。

| 类型 | 请求窗口 | 对比窗口 |
| --- | --- | --- |
| 日报 `day` | 昨日 00:00～今日 00:00 | 再前一日 |
| 周报 `week` | 上周一 00:00～本周一 00:00 | 再前一周 |
| 月报 `month` | 上月 1 日 00:00～本月 1 日 00:00 | 再上一自然月 |

查询复用时段统计：半开结束时刻换成整数秒闭区间 `[from, to-1]`，再交给 `resolve_period_window`。若起点早于 `retention_days`，实际起点被抬高，报告抬头写明 **clamped** 和实际起点。整段都在保留期之外时返回 400，并留下一条 `status=failed` 的记录和失败原因，**不编造利用率**。日期不合法，或窗口还没结束，返回 400 且不入库。

两种选窗方式：

- `as_of`（Unix 秒）只决定「今天」落在哪一天，于是取**上一个已经结束的完整周期**。不能拿它指定任意起止，也不能拿旧报告当数据源。
- `window_date`（`YYYY-MM-DD`，上海日历）指定已经结束的那一天、那一周或那一月。周报取该日所在的周一 00:00～下周一 00:00；月报取该日所在自然月。两者都给时，以 `window_date` 为准。

`as_of` / `window_date` **不是**生成时刻。实际计算时间单独记在 `generated_at`（库列和 `payload.generated_at`，正文写「实际生成时刻」）。补生成 9 月报告时，即使 `as_of` 落在 10 月 1 日，生成时刻也是这次计算的墙钟时间。v1 把 `as_of` 写进了「生成计算时刻」，已有正文不会自动改写，重新生成后才会换成实际时刻。旧行迁移时用原来的 `created_at` 填 `generated_at`（那就是当时的入库时间）。

## 内置调度

中心启动后有一条检查线程（约 20 秒）和一条生成线程。只在配置的那一分钟、进程还活着时触发。停机错过的分钟**不补**。缺的报告到页面或 API 上按窗口补生成。

| 周期 | 默认时刻（Asia/Shanghai） | 生成哪一段 |
| --- | --- | --- |
| 日报 | 每天 00:10 | 刚结束的那一天 |
| 周报 | 每周一 00:20 | 刚结束的周一 00:00～周一 00:00 |
| 月报 | 每月 1 日 00:30 | 刚结束的自然月 |

周报固定周一，月报固定 1 日，这两天不另配。同一分钟若配置成相同时刻，按日、周、月依次排队，月报算得久也不会吃掉下一分钟的检查。

进程若在这一分钟内重启，会再试一次；该窗口已经有行（成功或失败）就跳过，不覆盖。过了这一分钟就不再试。失败行留在列表里，等人工重新生成。

配置在 `config/center.json` 的 `report_schedule`。键可以整段省略，这时用上表默认值，**不会改动已有的 `token`、`retention_days`**。`enabled: false` 关掉全部；每个周期也可以单独 `enabled: false`。`hour` 必须是 0–23，`minute` 必须是 0–59，非法则中心拒绝启动。

```json
"report_schedule": {
  "enabled": true,
  "daily": {"enabled": true, "hour": 0, "minute": 10},
  "weekly": {"enabled": true, "hour": 0, "minute": 20},
  "monthly": {"enabled": true, "hour": 0, "minute": 30}
}
```

调度成功写入后调用 `center/report_schedule.py` 的 `deliver_report`。页面上手动「生成并保存」成功后也会调用。已在「设置」页启用并保存 Webhook 时，把报告正文发到企业微信、钉钉或飞书；未启用或没有 Webhook 时不发请求，报告照常入库。正文过长会截断。推送失败只记日志，不把这次生成改成失败。详见下文「报告推送」。

## 去重与覆盖

同一个 `(period_type, requested_from_ts, requested_to_ts)` 只保留一行。

- 页面和 `POST /api/v1/reports/generate` 再次生成同一窗口：**覆盖**这一行。`id` 不变，`generated_at` 改成这次的实际计算时间，`source` 改成 `manual`。v1 对同一窗口插过的更早重复行，在这次覆盖时删掉。其它窗口和 `metrics_history` 不动。
- 调度器发现该窗口已有任意一行：**跳过**，不覆盖手动报告，也不把失败行再算一遍。

列表每个窗口只显示最新一行。`source` 为 `scheduled`（定时）或 `manual`（手动），表示**当前这份正文**是谁生成的。

## 口径

与 [`docs/11-period-utilization.md`](11-period-utilization.md) 相同的样本加权（`sample_weighted`）和繁忙阈值（默认 CPU / 加速卡 ≥80%，`period_util` 可改）。

闲忙两极 **只看 CPU 和加速卡**。磁盘、内存不进闲忙。

| 判定 | 条件 |
| --- | --- |
| 高负荷 | 样本数 ≥ `report.min_samples`（默认 10），且 CPU 繁忙占比 ≥ `report.high_busy_gte`（默认 0.40，即 40%）或 CPU P95 ≥ `report.high_p95_gte`（默认 85%）；加速卡同样（有卡且样本够时） |
| 基本不用 | **生成时刻在线**且样本数 ≥ `report.min_samples`（默认 10）；CPU 平均 < `report.idle_avg_lt`（默认 5%）**且** 繁忙 < `report.idle_busy_lt`（默认 0.05，即 5%）；有加速卡则加速卡平均和繁忙也要低于同样两条线 |
| 样本不足 | 在线但样本不够，或有卡却没有加速卡样本。不判成空闲，也不编数 |
| 离线 | 生成时刻已离线。只写末次上报时间，**不把末次快照当实时用量** |

默认空闲线是平均 <5% 且繁忙 <5%（`report.idle_avg_lt` / `report.idle_busy_lt`）。更早的报告模板曾用 CPU 平均 <15%、加速卡平均 <10%，**未改配置时不用那两档**。这四条线和最少样本数在 `config/center.json` 的 `report` 段，也可以在设置页改；不写就用上面的默认。改 `period_util` 的繁忙线**不会**带动空闲线或高负荷线。已入库的旧报告不自动重算。

已落入高负荷的主机不再重复算作基本不用。

总览里的**窗口在线率**按「该窗口内至少有一条历史上报的主机 / 当前未隐藏主机数」计算。已隐藏的主机不进入分子、分母、排行和闲忙。没有单独的在线心跳表，采样中断会算成未上报。闲忙里的「离线」「基本不用」仍看生成时刻是否在线，不回溯窗口内状态。

主机排行、闲忙两极（高负荷 / 基本不用）和排行 CSV 带手工维护的**负责人**、**用途**。没填时正文写「未填」，CSV 里是空字符串。这些字段来自 `hosts.asset_meta`，采集端不会改写。已经入库的旧报告不会自动补上这两列，需要对该窗口再生成一次。隐藏发生在生成之后才生效：旧报告里仍可能出现后来被隐藏的主机。

磁盘单独成节：集群平均 / P95 / 最高；主机按窗口内最高 `disk_percent` 分档，线跟 `anomaly.disk_warn_percent` / `anomaly.disk_critical_percent`（默认 ≥85% 与 ≥95%）。内存偏高用 `anomaly.mem_warn_percent`（默认 85%）。多挂载的最满路径来自该窗口历史点里的 `disks[]`。历史里没有挂载列表时写「挂载样本不足」，不用生成时刻的快照回填过去。磁盘满不等于算力忙。

缺任何指标就写「样本不足」或「无加速卡」。

### 越界利用率

CPU、内存、磁盘、加速卡利用率只接受 **0～100%**（含两端）。负值、大于 100%、NaN、无穷大从平均、P95、繁忙占比里排除，不当成 0。报告写明排除了多少条、分在哪种指标、涉及哪些主机。上一周期若也有排除，总览会补一句，避免涨跌被脏样本带偏。

实例：中心机上的昇腾 310P3 曾留下最高约 1283784.7% 的样本，把 9 月集群加速卡平均抬到 112.6%。那些点已经在 `metrics_history` 里。重新生成报告时会排除它们，平均值回到有效样本。

采集端：`agent/metrics/npu.py` 在解析 `npu-smi` 时已用 `_util_valid` 丢掉 0～100 以外的 AICore，包括进程表 PID 被当成利用率的幽灵卡。当前 Agent 不会再把这种数上报进来。库里的旧点和尚未升级的旧 Agent 仍可能带着脏值；中心在汇总、时段统计和实时平均里同样丢掉越界值。实时异常判定把越界利用率标成「无效」，不把它当成高负荷。详情表显示「无效」。历史行不批量改写。

## 报告结构

1. 抬头（类型、时间窗、对比窗、口径、保留期裁剪）
2. 总览（窗口在线率、生成时刻在线/离线、集群指标、相对上一周期、一句风险）
3. 集群资源利用表（含越界排除说明）
4. 主机排行（CPU、加速卡、磁盘、内存偏高、异常/离线；含负责人、用途）
5. 闲忙两极（高负荷与基本不用同样带负责人、用途，便于决定回收）
6. 磁盘
7. 资源组（有分组时；组内仍是样本加权）
8. 诊断与建议（不根据部署日志推断「部署残留」）
9. 附录（样本数、实际生成时刻、来源、排除条数、裁剪、全量一览；完整排行见 CSV）

## API

监测台读接口仍直接打开。Agent 上报 Token 仍只校验 `POST /api/v1/metrics`，不会变成整站登录。若在设置页启用了**管理 Token**，`POST /api/v1/reports/generate` 和其他写接口要带请求头 `X-Management-Token`；未设置管理 Token 时与以前一样不校验。请求体里的 `source` 会被忽略，HTTP 生成一律是 `manual`。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/v1/reports/preview?period=day&window_date=2026-09-15` | 只返回将要使用的时间窗，不计算、不入库。`window_date` 可省略，这时仍是「上一个完整周期」 |
| POST | `/api/v1/reports/generate` | 正文 `{"period":"day"\|"week"\|"month","window_date":可选,"as_of":可选}`。成功覆盖该窗口的一行 |
| GET | `/api/v1/reports` | 已保存列表（每个窗口一行，按 `generated_at` 新的在前）。含 `source` / `status` / `error` / `generated_at` |
| GET | `/api/v1/reports/{id}` | JSON：`markdown` 与 `payload.sections` / `rankings` |
| GET | `/api/v1/reports/{id}/markdown` | 下载 Markdown |
| GET | `/api/v1/reports/{id}/rankings.csv` | 下载主机排行 CSV |

再次 GET 不会重算。窗口已知但算失败（例如整段超出保留期）时 HTTP 400，响应里仍带 `report`（`status=failed`，`error` 为原因）。

## 存储

表 `diagnostic_reports` 与主机库同一个 SQLite。列：周期、半开请求窗、是否裁剪、Markdown、完整 JSON，以及 `source`、`status`、`error`、`generated_at`。已有库只做 `ADD COLUMN`，不重写 `metrics_history`。

`retention_days` 的清理**只** `DELETE FROM metrics_history WHERE ts < 截止`。不删除 `diagnostic_reports`。报告正文因此可以在原始点过期之后继续打开；窗口若被保留期裁短，抬头仍写 clamped。设为 0 时不按天数删除，诊断窗口也不再被裁成 7 天。

历史点在原有趋势字段之外，有精简 `system.disks[]`（`mount` / `percent` / `used_gb` / `total_gb`），便于点名最满挂载。没有 `disks[]` 的旧点保持原样。时段统计的 avg/P95 仍只用汇总 `disk_percent`。越界的挂载利用率不参与「最满路径」。

单机读取上限在诊断报告里是 250000 行（时段统计接口默认仍是 60000）。触达上限时附录会写明，不从别的报告补数据。

## 页面

`/#reports`：选择日报 / 周报 / 月报，再选一个窗口日期（默认上一完整周期里的一天），看将生成的时间窗，生成并保存。左侧列表标出定时/手动、成功/失败和失败原因。打开一份后可以「重新生成」（覆盖同一窗口）。下载 Markdown 或排行 CSV。15 秒自动刷新不会清掉正在看的报告，也不会后台重算。

实时监控、时段统计的列（规格与实时利用率分开、离线不显示末次快照数字）不受这一页影响。时段统计的平均同样排除越界利用率，响应里多了 `excluded_samples`。

## 管理 Token 与报告推送

设置页：`/settings.html`。同一页配置全部中心参数，以及管理 Token、报告推送。

中心参数（保留天数、`anomaly`、`period_util`、`report` 空闲/高负荷线、`report_schedule`、监听地址、Agent 上报 Token 等）写入 `config/center.json`。文件里没写的键用代码默认，和 `config/center.example.json` 一致；字段说明见 `config/center.example.md`。设置页保存后，报告生成、异常着色、时段繁忙线、清理间隔和定时时刻**当前进程立即生效**。`host`、`port`、`data_dir` 写入文件后要重启 `monitor-center` 才换绑定，这不是升级。接口不回显 Agent 上报 Token 明文。

管理 Token 和报告推送仍写在中心库 `admin_settings`，不进 `config/center.json`，升级保留 `data/` 时会留下来。读取接口不回显管理 Token 明文，Webhook 继续打码。

### 管理 Token

- 未设置：部署、改主机、生成报告、保存设置等写接口与以前一样不校验。监测台从中心 IP 打开就能完成第一次保存。请在端口对别人可见之前先设上；这段时间里别人也能抢先设置。
- 已设置：上述写接口必须带请求头 `X-Management-Token`。错误或缺失返回 401，`code` 为 `management_token_required`。读接口（主机列表、报告、导出、设置状态）仍开放。`GET /api/v1/settings` 只说明是否已设置，不返回明文，也不返回哈希。
- 保存的是 SHA-256。页面保存后清空输入框。当前标签页用顶栏黄条记住明文，关掉标签页要再填。
- 清空也要带当前 Token。忘记时在中心机执行：`sqlite3 data/monitor.db "DELETE FROM admin_settings WHERE key='management_token_sha256';"`，然后立刻重新设置。
- 首次启动前可以设环境变量 `MONITOR_MANAGEMENT_TOKEN`。只在库里还没有管理 Token 时写入哈希；已有则忽略，也不写回 `center.json`。短于 8 位则中心拒绝启动。
- Agent 的 `token` 仍只校验 `POST /api/v1/metrics`。管理 Token 不能拿来上报，上报 Token 也不能拿来改设置。

### 报告推送

`POST /api/v1/settings` 可同时或单独保存：

| 字段 | 说明 |
| --- | --- |
| `push_enabled` | `true` / `false` |
| `push_channel` | `wecom` 企业微信、`dingtalk` 钉钉、`feishu` 飞书 |
| `push_webhook_url` | 留空不要提交。提交则覆盖。只接受 http/https |
| `clear_push_webhook` | `true` 时删掉地址，并在未另行指定时关闭推送 |

读取接口只返回渠道、是否启用、以及打码后的地址（主机名 + 密钥末四位）。

启用且地址存在时，`deliver_report` 发送：

- 企业微信：`msgtype=markdown`
- 钉钉：`msgtype=markdown`。当前不算加签，请在机器人安全设置里用自定义关键词，关键词包含「诊断」
- 飞书：`msg_type=text`

定时任务和手动生成成功后都会发。未配置则直接返回，不访问网络。对方返回非 0 错误码时，报告仍然保留，接口里的 `push.ok` 为 false。

## 升级中心

已安装的中心在工作站执行 `scripts/push_upgrade.sh`（中心上跑的是这次包里的 `scripts/upgrade_center.sh`）。先 `--check-only`，通过后再正式升级。脚本保留 `config/center.json`、`data/` 和已放置的便携包；健康检查失败只回滚代码和单元，不覆盖数据库。不要把手写 `systemctl restart` 或直接执行 `scripts/lib/sync_tree.sh` 当成升级。步骤见 `docs/14-ops-runbook.md`。

不必为了启用调度去改配置。要改时刻、阈值或保留天数，优先用设置页，保存后当前进程就按新值工作。手改中心机上的 `config/center.json` 之后仍要重启 `monitor-center` 才会重读文件；这只重读配置，不换代码，也不是升级。

不要清空数据库。Agent 仍只走 `/deploy.html` 与中心机 `deploy_runner` / `deploy_fleet.sh`，不要在笔记本上用 sshpass/scp 旁路安装。采集端的越界过滤随 Agent 升级生效；不升级 Agent 时，中心侧报告仍然排除已入库的脏样本。
