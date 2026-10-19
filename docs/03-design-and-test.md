# 简易多机监控系统 — 详细设计与测试计划

## 1. API 详细设计

### 1.1 POST `/api/v1/metrics`

**请求体**：见 SRS 5.1。

**响应**：

- `200`：`{"ok": true}`
- `400`：字段缺失或 JSON 非法
- `401`：Token 不匹配
- `405`：方法不允许

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
      "gpu_count": 4,
      "gpu_util_avg": 55.0
    }
  ]
}
```

### 1.3 GET `/api/v1/hosts/{host_id}`

返回完整最新 `payload`、`online`，以及手工 `asset` 和采集 `hardware`。已隐藏的主机仍可按 id 打开，便于恢复前查看。

### 1.3.1 手工资产、隐藏、导入

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/hosts/{id}/meta` | 合并手工字段。给出的空字符串清空。未知键忽略。含密码、用户名、token 等键则 400，不写库 |
| POST | `/api/v1/hosts/{id}/hide` | `hidden=1`。历史上报和分组关系保留。Agent 再上报不会自动取消隐藏。正文 `remove_deploy_target: true` 时删除部署清单里 host_id 相同，或 IP 对得上且清单 host_id 为空 / 等于 IP / 嵌着同一 IP 的目标 |
| POST | `/api/v1/hosts/{id}/restore` | 取消隐藏 |
| DELETE | `/api/v1/hosts/{id}?confirm=delete` | 删除主机行、`metrics_history` 和分组成员。缺少 `confirm=delete` 返回 400，一行不删。`remove_deploy_target=1` 同上 |
| POST | `/api/v1/hosts/meta/import` | 正文为 CSV、xlsx，或 JSON `{"csv":"..."}` / `{"rows":[...]}`。默认只预览。`?apply=1` 或 JSON `"apply": true` 才写入。按 IP 匹配已有主机（手工展示地址、历史 IP、展示地址、host_id 内嵌 IP、最近来源 IP、上报网卡）。未匹配不新建。同一 IP 多台主机则该行不写。同一 IP 多行以最后一行为准。空单元格不覆盖。`硬盘/型号`：纯容量进 `disk`，其余进 `model`。表头「地址」只用来匹配，不写进展示地址。凭据列不出现在预览和库里 |
| POST | `/api/v1/hosts/{id}/readdress` | 正文 `{"new_ip","note","apply"}`。`apply` 不是 `true` 时只返回计划。`apply: true` 把手工 `address` 设为新 IP，把当前展示地址追加进 `previous_ips`（带上海日期和 note），`host_id` 与 `metrics_history` 不动。新 IP 若恰有一个部署目标，把它的 `host_id` 改成这台主机并取消停用，密码不动；旧 IP 上属于这台主机、或 `host_id` 就是这台主机且 IP 不是新地址的目标设 `retired=1`，不删除。多个目标共用新 IP、新 IP 无效、或与当前展示地址相同，则 400 且不写库 |

`asset` 字段：`owner` 负责人、`purpose` 用途、`address` 手工展示地址、`cpu` / `memory` / `disk` / `gpu` / `model` 型号或资产编号、`notes` 备注，以及导入可带的 `name`、`category`、`vm_host`、`monitor_flag`。`previous_ips` 是 `[{"ip","date","note"}]`，不是普通字符串字段；只改其他资产字段时仍保留它。

部署目标 `POST /api/v1/deploy/targets/{id}` 可以改 `ip` 和 `host_id`。IP 必须是可用 IPv4；`host_id` 不能空、不能含空白或斜杠。改到的 IP 若已被另一个目标使用，返回 400，该行不变。未提交或空字符串的 `ssh_password` 保持原值。`deploy_targets.retired` 为可空整数，`1` 表示停用：不进入展示地址映射，也不进入部署任务。

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

见 `docs/11-period-utilization.md`。相对窗口 `minutes` 保持兼容；绝对窗口 `from_ts`/`to_ts`（Unix 秒）成对使用并裁剪到保留期。集群汇总为**样本加权**（合并各机窗口内样本后再聚合），不含已隐藏主机。各主机带 `owner` / `purpose`。

### 1.6 使用诊断报告 `/api/v1/reports`

见 `docs/13-diagnostic-reports.md`。

- `GET /api/v1/reports/preview?period=day|week|month`：只解析 Asia/Shanghai 半开窗口。可选 `window_date=YYYY-MM-DD`
- `POST /api/v1/reports/generate`：按原始历史聚合。同一 `(period, 半开窗口)` 覆盖一行，不另起版本。调度器调用时 `replace=false`，已有则跳过
- `GET /api/v1/reports`、`GET /api/v1/reports/{id}`：读库存。列表带 `source`（scheduled/manual）、`status`（ok/failed）、`error`、`generated_at`。`{id}/markdown` 与 `{id}/rankings.csv` 为下载
- 调度：中心进程内，默认每天 00:10、每周一 00:20、每月 1 日 00:30（上海时间）。只在那一分钟触发，不补跑停机错过的时刻
- `generated_at` 是实际计算时间，与 `as_of` / 窗口边界分开
- 0～100% 以外的利用率不进入平均 / P95 / 繁忙占比，报告写明排除条数和主机
- 空闲判定：在线且样本足够时，CPU 平均 <5% 且繁忙 <5%；有加速卡则同样 <5%。高负荷为繁忙占比 ≥40% 或 P95 ≥85%（CPU 或加速卡）。磁盘不进闲忙。排行、闲忙和 CSV 含负责人、用途。已隐藏主机不进入报告
- 与监测台其它 GET 一样不校验 Agent Token

---

## 2. Agent 调度设计

```
load_config
loop:
  collect_system()
  collect_gpu()
  resolve_host_type()
  send()
  sleep(interval)
```

异常隔离：单次采集或发送异常 catch 后打日志，继续下一轮。

---

## 3. Web 页面信息架构

1. **顶部**：产品名 + 刷新 + 页签（实时监控 / 时段统计 / 使用诊断）+ 客户端部署入口
2. **实时监控**：异常汇总、集群卡片、主机表（可排序、按资源组 / 负责人筛选，搜索含用途）。列把规格和实时利用率分开：加速卡型号数量 | 加速卡使用率；磁盘% | 磁盘数；另有负责人、用途。离线行不填实时数字。详情可改手工资产，旁边只读展示采集规格；可隐藏或彻底删除。工具条可打开已隐藏列表和资产表导入预览
3. **时段统计**：集群与各主机的平均 / P95 / 繁忙占比，可导出 CSV
4. **使用诊断**：选择已结束的窗口生成或覆盖报告，看定时/手动与成功/失败，下载 Markdown 与排行 CSV
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
| TC-ONLINE-01 | 停止上报 > offline_seconds | online=false |
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
