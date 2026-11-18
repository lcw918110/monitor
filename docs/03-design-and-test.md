# 简易多机监控系统 — 详细设计与测试计划

## 1. API 详细设计

### 1.1 POST `/api/v1/metrics`

**请求体**：见 SRS 5.1。

**响应**：

- `200`：`{"ok": true, "status": "inserted"|"duplicate", "spool_retention_hours": 24}`
- `400`：字段缺失或 JSON 非法
- `401`：Agent 上报 Token 不匹配。与设置页的管理 Token 不是同一把钥匙
- `405`：方法不允许

`timestamp` 有值时，历史行的 `ts` 用这个采集时间，`received_at` 才是中心收到的时刻。缺省时间戳的旧客户端仍用服务器时间。同一 `(host_id, timestamp)` 再来一笔记为 `duplicate`，不改第一笔的 `backfill` 标记。补报若比当前快照更旧，不把 `last_seen` 和末次快照改回去。

### 1.1.1 POST `/api/v1/metrics/batch`

补报入口。同样只校验 Agent `token`，不要求管理 Token。正文：

```json
{
  "host_id": "gpu-01",
  "token": "optional-agent-token",
  "samples": [],
  "overflows": [{"from_ts": 1, "to_ts": 2, "dropped": 3, "reason": "retention"}]
}
```

`samples` 最多 100 条，每条是一笔普通上报，并带 `backfill: true` 和原来的 `timestamp`。采集端每次实时上报成功后最多再送一批（默认 80），避免断网恢复时把中心打满。`overflows` 是本地队列丢掉的范围，写入 `spool_overflows`，重复范围不另计。响应 `200` 带 `accepted`、`duplicate`、`rejected`、`stored_overflows` 和 `spool_retention_hours`。

### 1.2 GET `/api/v1/hosts`

默认不含 `hidden=1` 的主机。`?hidden=1` 只返回已隐藏，`?include_hidden=1` 含全部。每台带 `owner`、`purpose`、`asset`（手工）和 `hardware`（Agent 采集规格）。

**响应示例**：

```json
{
  "hosts": [
    {
      "host_id": "gpu-01",
      "hostname": "gpu-01",
      "host_type": "gpu",
      "online": true,
      "last_seen": 1722096000,
      "cpu_percent": 12.3,
      "mem_percent": 45.0,
      "disk_percent": 61.0,
      "disk_read_mbps": 1.2,
      "disk_write_mbps": 8.4,
      "disk_io_util_percent": 6.0,
      "accel_count": 4,
      "accel_summary": "NVIDIA ×4 A100",
      "accel_util_avg": 55.0,
      "data_guard": {"marked": true, "attention": false, "kinds": []}
    }
  ]
}
```

页面口径用 `accel_summary` 表示加速卡型号和数量，用 `disk_read_mbps` / `disk_write_mbps` 表示磁盘读写，用 `data_guard.marked` 表示「重点数据机」徽章。离线行不把这些数字当成当前利用率。

### 1.3 GET `/api/v1/hosts/{host_id}`

返回完整最新 `payload`、`online`，以及手工 `asset` 和采集 `hardware`。已隐藏的主机仍可按 id 打开，便于恢复前查看。

### 1.3.1 手工资产、隐藏、导入

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/hosts/{id}/meta` | 合并手工字段。给出的空字符串清空。未知键忽略。含密码、用户名、token 等键则 400，不写库。`previous_ips` 缺省不改，`[]` 清空，非数组或无效 IP 为 400 且不写库 |
| POST | `/api/v1/hosts/{id}/hide` | `hidden=1`。历史上报和分组关系保留。Agent 再上报不会自动取消隐藏。正文 `remove_deploy_target: true` 时删除部署清单里 host_id 相同，或 IP 对得上且清单 host_id 为空 / 等于 IP / 嵌着同一 IP 的目标 |
| POST | `/api/v1/hosts/{id}/restore` | 取消隐藏 |
| DELETE | `/api/v1/hosts/{id}?confirm=delete` | 删除主机行、`metrics_history` 和分组成员。缺少 `confirm=delete` 返回 400，一行不删。`remove_deploy_target=1` 同上 |
| POST | `/api/v1/hosts/meta/import` | 正文为 CSV、xlsx，或 JSON `{"csv":"..."}` / `{"rows":[...]}`。默认只预览。`?apply=1` 或 JSON `"apply": true` 才写入。按 IP 匹配已有主机（手工展示地址、历史 IP、展示地址、host_id 内嵌 IP、最近来源 IP、上报网卡）。未匹配不新建。同一 IP 多台主机则该行不写。同一 IP 多行以最后一行为准。空单元格不覆盖。`硬盘/型号`：纯容量进 `disk`，其余进 `model`。表头「地址」只用来匹配，不写进展示地址。凭据列不出现在预览和库里 |
| POST | `/api/v1/hosts/{id}/readdress` | 正文 `{"new_ip","note","apply"}`。`apply` 不是 `true` 时只返回计划。`apply: true` 把手工 `address` 设为新 IP，把当前展示地址追加进 `previous_ips`（带上海日期和 note），`host_id` 与 `metrics_history` 不动，并把改动前的目标 `host_id` / `retired` 记进 `readdress_undo`。新 IP 若恰有一个部署目标，把它的 `host_id` 改成这台主机并取消停用，密码不动；旧 IP 上属于这台主机、或 `host_id` 就是这台主机且 IP 不是新地址的目标设 `retired=1`，不删除。多个目标共用新 IP、新 IP 无效、或与当前展示地址相同，则 400 且不写库 |
| POST | `/api/v1/hosts/{id}/readdress/undo` | 正文 `{"apply"}`。没有 `readdress_undo` 时 400。记录里的目标 id 已删除则 400 且不写库。`apply: true` 按记录恢复每个目标的 `host_id` 和 `retired`，手工 `address` 清空，只去掉这次追加的那条 `previous_ips`，然后清除撤销记录 |

`asset` 字段：`owner` 负责人、`purpose` 用途、`address` 手工展示地址、`cpu` / `memory` / `disk` / `gpu` / `model` 型号或资产编号、`notes` 备注，以及导入可带的 `name`、`category`、`vm_host`、`monitor_flag`。`previous_ips` 是 `[{"ip","date","note"}]`，不是普通字符串字段；只改其他资产字段时仍保留它。

部署目标 `POST /api/v1/deploy/targets/{id}` 可以改 `ip`、`host_id` 和 `retired`（`true` / `false` / `1` / `0`）。IP 必须是可用 IPv4；`host_id` 不能空、不能含空白或斜杠。`retired` 非法时 400，且不会去做网络探查。改到的 IP 若已被另一个目标使用，返回 400，该行不变。未提交或空字符串的 `ssh_password` 保持原值。只提交 `retired` 时 IP、host_id 和密码都不变。`deploy_targets.retired` 为可空整数，`1` 表示停用：不进入展示地址映射，也不进入部署任务。部署页每行有「停用 / 恢复」。

### 1.4 GET `/api/v1/stats`

```json
{
  "host_total": 10,
  "host_online": 8,
  "gpu_hosts": 4,
  "gpu_cards": 16,
  "avg_cpu_percent": 21.5,
  "avg_mem_percent": 48.2,
  "avg_gpu_util_percent": 33.0
}
```

统计口径：只统计未隐藏主机。平均值只算其中 **在线** 的；GPU 卡数统计这些主机最新快照中的卡（含刚离线，便于盘点资产）。卡片总数以最新快照为准。

### 1.5 时段利用 `GET /api/v1/hosts/{id}/period-stats` 与 `GET /api/v1/period-stats`

见 `docs/11-period-utilization.md`。相对窗口 `minutes` 保持兼容；绝对窗口 `from_ts`/`to_ts`（Unix 秒）成对使用并裁剪到保留期。集群汇总为**样本加权**（合并各机窗口内样本后再聚合），不含已隐藏主机。各主机带 `owner` / `purpose`。每台主机的窗口边界（全历史最早 `ts`、窗口起点之前最近一条、窗口起点之前最近一条非补报）按 `idx_metrics_host_ts (host_id, ts)` 分三次读取。

### 1.6 使用诊断报告 `/api/v1/reports`

见 `docs/13-diagnostic-reports.md`。

- `GET /api/v1/reports/preview?period=day|week|month`：只解析 Asia/Shanghai 半开窗口。可选 `window_date=YYYY-MM-DD`
- `POST /api/v1/reports/generate`：按原始历史聚合。同一 `(period, 半开窗口)` 覆盖一行，不另起版本。调度器调用时 `replace=false`，已有则跳过
- `GET /api/v1/reports`、`GET /api/v1/reports/{id}`：读库存。列表带 `source`（scheduled/manual）、`status`（ok/failed）、`error`、`generated_at`。`{id}/markdown` 与 `{id}/rankings.csv` 为下载
- 调度：中心进程内，默认每天 00:10、每周一 00:20、每月 1 日 00:30（上海时间）。只在那一分钟触发，不补跑停机错过的时刻
- `generated_at` 是实际计算时间，与 `as_of` / 窗口边界分开
- 0～100% 以外的利用率不进入平均 / P95 / 繁忙占比，报告写明排除条数和主机
- 空闲判定：在线且样本足够时，CPU 平均 <5% 且繁忙 <5%；有加速卡则同样 <5%。高负荷为繁忙占比 ≥40% 或 P95 ≥85%（CPU 或加速卡）。磁盘不进闲忙。排行、闲忙和 CSV 含负责人、用途。已隐藏主机不进入报告
- 读接口不校验 Agent Token。启用管理 Token 后，生成报告这一条 POST 要带 `X-Management-Token`；未启用时不校验。成功响应可带 `push`（未配置时 `skipped: true`）。`push.ok` 为 false 时，报告 `status` 仍是 `ok`

### 1.7 设置 `GET/POST /api/v1/settings` 与 `POST /api/v1/settings/push-test`

`GET /api/v1/settings` 返回 `settings` 和 `center`。`settings` 说明管理 Token 是否已设置、诊断报告推送是否启用、渠道（`wecom` 企业微信 / `dingtalk` 钉钉 / `feishu` 飞书）、打码后的 Webhook（主机名 + 密钥末四位），以及 `push_last`。不返回管理 Token 明文、Agent 上报 Token 明文、Webhook 原文，也不返回哈希。

`push_last` 还没有记录时为 `null`。有记录时：

| 字段 | 说明 |
| --- | --- |
| `kind` | `test` 测试发送，`report` 报告推送 |
| `kind_label` | 「测试发送」或「报告推送」 |
| `at` | 北京时间 `YYYY-MM-DD HH:MM:SS` |
| `ok` | 对方接受为 true |
| `skipped` | 未启用或没有 Webhook 时为 true，且没有访问网络 |
| `reason` | 未发出时为 `not_configured` |
| `error` | 失败原因，不含 Webhook |
| `summary` | 设置页展示的那一句 |

`POST /api/v1/settings` 保存管理 Token、推送开关 / 渠道 / Webhook，以及 `center` 里的中心参数。管理 Token 不是保存 Webhook 或启用推送的前提。未设置管理 Token 时写接口不校验；已设置则要请求头 `X-Management-Token`，缺失或错误为 401，`code` 为 `management_token_required`。

`POST /api/v1/settings/push-test` 与上面同一套鉴权。它只发一条短消息，三个渠道的正文都含「诊断」。企业微信为 markdown，钉钉为 markdown 且没有加签字段，飞书为 text。不生成、不保存诊断报告。未启用或没有 Webhook 时返回 200，`push.skipped` 为 true，`push.reason` 为 `not_configured`。传输失败再试两次（约 0.4 秒、0.8 秒），单次超时 10 秒；对方返回非 0 不重试。成功、失败和未发出都会更新 `push_last`。

定时生成和手动「生成并保存」成功后同样更新 `push_last`。推送失败不改变报告 `status`。

---

## 2. Agent 调度设计

```
load_config
open data/spool          # 安装目录下，重启和覆盖安装都留着
loop:
  collect_system()
  collect_accelerators()   # host_type=cpu 时跳过；否则含厂商工具与 PCI 回退
  resolve_host_type()
  send()                    # 短重试仍失败则入队，带原来的 timestamp
  on success:
    采用响应里的 spool_retention_hours
    POST /api/v1/metrics/batch 一批（默认 80，标记 backfill）
  sleep(interval)
```

队列超过 `spool_retention_hours`（默认 24）或条数上限时丢掉最旧样本，把范围记在 `overflows.json`，下一次补报带给中心。异常隔离：单次采集或发送异常 catch 后打日志，继续下一轮。采集还没形成样本时不入队。

---

## 3. Web 页面信息架构

1. **顶部**：产品名 + 刷新 + 一条顶栏（实时监控 / 主机 / 时段统计 / 使用诊断 / 运维 / 设置）。运维仍打开 `/deploy.html`
2. **实时监控**：今日关注是类别和台数，判定是一排计数，集群统计是数字卡片，下面是主机表（可排序、按资源组 / 负责人筛选，搜索含用途）。列把规格和实时利用率分开：加速卡型号数量 | 加速卡使用率；磁盘% | 磁盘数；另有负责人、用途。离线行不填实时数字。名词解释在对应标题、列名或状态词上悬停，移开就消失。详情可改手工资产，旁边只读展示采集规格；可隐藏或彻底删除。工具条可打开已隐藏列表和资产表导入预览
3. **时段统计**：汇总条给出「离线（无数据）」和「断网（已补报）」的段数与时长。左侧列表第一行是「集群」，点它看集群平均 / P95 / 繁忙占比；点一台主机只换右侧详情。可导出中文 CSV。趋势悬停是上海时间；空档为横线。定义在对应名字上悬停
4. **使用诊断**：选择已结束的窗口生成或覆盖报告，看定时/手动与成功/失败/无数据，下载 Markdown 与排行 CSV。调度在「使用诊断」标题上悬停。预览里口径悬停在对应的词上；带表的附录点章节名才展开。下载的 Markdown 仍是全文。没有上报时报告仍留在列表里，右侧是空状态
5. **主机详情**：在线时展示实时用量与分盘；离线时标明末次快照不是实时。手工资产可编辑，采集规格只读，并提供隐藏与彻底删除

风格：清晰运维控制台，中文标签，数字突出。磁盘汇总口径见 `docs/12-disk-mounts.md`。地址口径见 `center/hostaddr.py`。

---

## 4. 测试计划

| 用例 ID | 步骤 | 期望 |
| --- | --- | --- |
| TC-DEP-01 | 启动中心端访问 `/` | 页面 200，可见标题 |
| TC-AGT-01 | Agent 上报应用指标 | hosts 出现该机且 online |
| TC-AGT-02 | 伪造含 gpus 的上报 | 详情可见 GPU 行 |
| TC-AGT-03 | 错误 Token | 401，不入库 |
| TC-AGT-04 | 无 nvidia-smi 主机 | gpus 空数组，仍在线 |
| TC-UI-01 | 打开统计 API | 字段齐全且与列表可对账 |
| TC-UI-02 | 点击主机 | 详情切换正确 |
| TC-ONLINE-01 | 停止上报 > offline_seconds | online=false，实时状态仍叫「离线」 |
| TC-SPOOL-01 | 发送失败后重启进程再读 `data/spool` | 原 timestamp 还在 |
| TC-SPOOL-02 | 队列超过条数或保留时间 | 丢掉最旧样本，并留下溢出事件 |
| TC-SPOOL-03 | 带旧 timestamp 的补报 | 历史 `ts` 是采集时间；`(host_id, ts)` 不重复；不把更新的快照改旧 |
| TC-SPOOL-04 | 窗口里既有空档又有补报点 | 时段概览和诊断正文分开写「离线（无数据）」与「断网（已补报）」 |
| TC-PERIOD-01 | 空主机、只有窗口内的点、窗口前有实时点、窗口前最近一条是补报、最早点在窗口之外 | `_history_edges` 的三元组与按主机聚合的定义一致；查询计划使用 `idx_metrics_host_ts`，带 `ts` 上界的语句计划里有 `ts<?` |
| TC-RET-01 | 写入超期历史后触发清理 | 旧记录删除 |
| TC-REPORT-01 | 生成日报后删除原始历史再 GET | 正文与入库时一致，不重算 |
| TC-REPORT-02 | CPU 平均 14% 或加速卡平均 9% | 不算基本不用（阈值 5%） |
| TC-REPORT-03 | 已有日报文本再生成月报 | 月报正文不包含该日报标记 |
| TC-ASSET-01 | 写入负责人后再上报 | 手工字段仍在，采集 CPU 型号更新 |
| TC-ASSET-02 | CSV/xlsx 含密码列，先预览再 apply | 凭据不出现在响应和库里；按 IP 写入；空单元格不覆盖 |
| TC-ASSET-03 | 隐藏、恢复、无 confirm 的 DELETE、`confirm=delete` | 隐藏不计统计和报告；无 confirm 不删历史；确认后历史消失 |
| TC-ASSET-04 | 隐藏一台有样本的主机后生成报告 | 排行含负责人/用途，不含被隐藏主机 |
| TC-REPORT-04 | 调度在 00:11 或次日中午检查 | 不补生成错过的 00:10 |
| TC-REPORT-05 | 同一窗口生成两次 | 仍是一行，id 不变，`generated_at` 更新 |
| TC-REPORT-06 | `as_of` 取旧窗口、`generated_at` 取现在 | 正文「实际生成时刻」是后者 |
| TC-REPORT-07 | 加速卡样本 >100% | 不进入平均，正文点名主机和排除条数 |
| TC-REPORT-08 | 清理超期 `metrics_history` | `diagnostic_reports` 仍在 |
| TC-PUSH-01 | 未启用或没有 Webhook 时「测试发送」 | 不访问网络；`skipped`，原因为 `not_configured`；设置页能看到未发出 |
| TC-PUSH-02 | 企业微信、钉钉、飞书各发一条测试消息 | 正文含「诊断」；钉钉报文没有 `sign`；不生成诊断报告 |
| TC-PUSH-03 | 测试发送或报告推送之后 GET `/api/v1/settings` | `push_last` 有北京时间、成功或失败原因，以及测试发送或报告推送；响应里没有 Webhook 原文 |
| TC-PUSH-04 | 已设置管理 Token 后测试发送 | 不带 `X-Management-Token` 为 401；带上才发送。未设置 Token 时与其它写接口一样放行 |
| TC-PUSH-05 | 推送请求失败后再看报告 | 报告生成仍成功；错误文本、日志和 `push_last` 都不含 Webhook |
| TC-PUSH-06 | 传输失败，以及对方返回非 0 | 传输失败再试两次；对方拒绝只请求一次 |

---

## 5. 工程目录约定

```text
monitor/
  docs/                 需求、架构、设计与测试
  center/               中心端代码与静态页
  agent/                采集端
  config/               配置样例
  scripts/              部署脚本
  tests/                基础自测
  README.md             使用说明
```

## 6. 重点数据守护

版本 v1.13.0。只对设置里标成重点数据机的主机生效，主机设置优先于资源组。

上报 `POST /api/v1/metrics` 成功时，响应增加 `data_guard`。未标记为 `{enabled: false}`。已标记时带上路径、允许网段、外传阈值和白名单，采集端缓存到本机状态目录，下一轮才按它采集。状态目录默认是 `~/.monitor-agent/data-guard`，也可用 `data_guard_state_dir` 或环境变量 `MONITOR_AGENT_STATE`。

采集结果放在请求体的 `data_guard.observation` 里：路径的文件数、总字节、最近修改时间、新归档的文件名和大小、可移动挂载、连接汇总、`net_tx_mbps` 的短窗口。没有文件内容。中心用当前策略重算事件，历史只留摘要和 `data_guard_events` 表。未标记的主机会把这段从入库报文里拿掉。

短时发送失败时，整笔指标（含 `data_guard`）进入 v1.12 的 `data/spool`，入口是 `agent.spool.enqueue_data_guard(spool, payload)`。没有 `agent.report_spool` 模块，也不另做 24 小时指标库。补报批次同样按当前策略重算事件。

页面：实时列表徽章「重点数据机」；今日关注增加「数据外传异常」。时段统计和使用诊断列出外传异常、路径完整性变化、未允许网段外连、可移动磁盘挂载的次数和时长。这些次数不进入闲忙，也不改变 `disk_percent`。

设置接口 `GET/POST /api/v1/data-guard`。POST 与其它写接口一样要管理 Token。导出仍是中文表头，JSON 键为 `data_guard_marked`、`egress_anomaly_count` 等英文名。

做不到的事写在「重点数据守护」标题的悬停里，设置页、详情和字段说明是同一句话：不能拦截；看不到加密隧道里的内容；不能指出哪一个文件离开了；备份和同步必须进白名单。鼠标移入显示，移开隐藏。

| 用例 | 期望 |
| --- | --- |
| TC-GUARD-01 | 未标记主机不徽章、事件不入库；标记主机或资源组后徽章出现，主机设置可关掉组策略 |
| TC-GUARD-02 | 路径个数和字节一起按比例下降，或新出现大归档，记为路径完整性变化。读失败不报变小 |
| TC-GUARD-03 | 允许网段内的地址不算未允许网段外连，网段外的算 |
| TC-GUARD-04 | 持续 Mbps 或窗口发送量超过设置时记外传异常 |
| TC-GUARD-05 | 白名单目的或进程能压住同一次备份造成的外传和未允许网段外连 |
| TC-GUARD-06 | 报文和事件 JSON 里没有文件内容字段 |
