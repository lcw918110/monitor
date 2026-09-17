# 时段利用统计（FR-UTIL-PERIOD）

按时间段统计服务器（单机 / 集群）的资源利用情况。在原有相对窗口 `minutes` 之上，增加绝对时间范围、P95、繁忙占比，以及多机汇总与 CSV 导出。

## 时间窗

| 方式 | 参数 | 说明 |
| --- | --- | --- |
| 相对（兼容） | `minutes` | 从当前时刻往前 N 分钟。预设：60 / 120 / 360 / 1440 / 10080（1h～7d）。省略时单机/集群时段接口默认 **120**。 |
| 绝对 | `from_ts` & `to_ts` | Unix 秒。必须成对出现，且 `from_ts < to_ts`。 |

规则：

1. 同时提供绝对范围与 `minutes` 时，**以 `from_ts`/`to_ts` 为准**。
2. 窗口会被裁剪到历史保留期（默认 `retention_days=7`）与当前时刻：过早的 `from_ts` 提到保留起点，未来的 `to_ts` 收到 `now`。
3. 裁剪后若区间为空（整段都在保留期之前），返回 400：`时间范围已超出历史保留期`。
4. 响应里的 `window` 是裁剪后的查询窗；顶层 `from_ts`/`to_ts`（单机）仍表示**实际有样本**的首末时间，便于对照。

## API

### 单机（增强，旧参数仍可用）

```
GET /api/v1/hosts/{id}/period-stats?minutes=120
GET /api/v1/hosts/{id}/period-stats?from_ts=1758000000&to_ts=1758007200
GET /api/v1/hosts/{id}/period-stats?minutes=120&busy_cpu=80&busy_accel=80
```

### 集群 / 多机

```
GET /api/v1/period-stats?minutes=120
GET /api/v1/period-stats?from_ts=...&to_ts=...
GET /api/v1/period-stats?minutes=1440&host_ids=gpu-01,app-01
```

`host_ids` 为可选逗号分隔过滤。响应包含：

- `hosts[]`：每台主机的聚合
- `cluster.metrics`：集群汇总
- `rollup`: `"sample_weighted"`

### 导出 CSV

```
GET /api/v1/export/period-stats.csv?minutes=120
```

行为与集群接口同一套查询参数。首行 `__cluster__` 为汇总，其后每台主机一行；列为各指标的 avg/min/max/p95/busy_ratio。

历史趋势（详情折线）也可带同一时间窗：

```
GET /api/v1/hosts/{id}/history?minutes=120&limit=240
GET /api/v1/hosts/{id}/history?from_ts=...&to_ts=...&limit=240
```

无 `minutes`/`from_ts` 时 history 仍默认 60 分钟。

## 指标与聚合

每个指标返回：

| 字段 | 含义 |
| --- | --- |
| `avg` / `min` / `max` | 算术平均、最低、最高（与旧接口兼容） |
| `p95` | 线性插值 95 分位，`rank = 0.95 * (n-1)` |
| `busy_ratio` | 样本中 **≥ 阈值** 的比例（0～1）；该指标无阈值则为 `null` |
| `sample_count` | 该指标有效样本数 |

统计字段：

- CPU / 内存 / 磁盘 利用率、`load1`
- 网络入/出向 Mbps
- 加速卡平均利用率、时段内最高温度
- **相对额定**：`net_rx_percent` / `net_tx_percent`（实时吞吐 / `net_rated_mbps`）；历史压缩里会写入额定与占比。旧点只有 Mbps 时，用该机**最新快照**的 `net_rated_mbps` 回填后再算占比。

## 繁忙阈值

默认：`cpu_percent >= 80`、`accel_util_avg >= 80`。

- 配置：`config/center.json` 的 `period_util.busy_cpu_percent` / `busy_accel_percent`（可选 `busy_mem_percent`、`busy_disk_percent`）
- 查询覆盖：`busy_cpu`、`busy_accel`、`busy_mem`、`busy_disk`；负值表示该指标不算繁忙占比

## 集群汇总口径（样本加权）

**合并所有主机在窗口内的原始样本后再算 avg / p95 / busy_ratio**；min/max 取全局最低/最高。

因此上报更密、在线更久的主机权重大。若改成「先按机平均再对主机等权平均」，两台机（3 个 20% 点 vs 1 个 80% 点）会得到 50% 而不是 35%。本实现取 35%。

## UI

监测台顶部页签分 **「实时监控」** 与 **「时段统计」**，不再把时段块堆在实时主机列表同一页。

**实时监控**：异常汇总、集群即时卡片、主机列表与实时详情。

**时段统计**（`/#period`）：

1. 预设 1h/2h/6h/24h/7d，或自定义起止时间
2. 可调 CPU / 加速卡繁忙阈值
3. 集群汇总表 + 各主机表（平均 / P95 / 繁忙占比等）
4. 「导出 CSV」下载当前窗口报表
5. 点选主机查看该机时段指标表与趋势（与本页时间窗相同）

## 在运行中的中心上核对

```bash
# 相对窗口（旧行为）
curl -s "http://127.0.0.1:8080/api/v1/hosts/<host_id>/period-stats?minutes=120" | python3 -m json.tool

# 绝对窗口
curl -s "http://127.0.0.1:8080/api/v1/period-stats?from_ts=$(date -d '2 hours ago' +%s)&to_ts=$(date +%s)"

# 集群 + 导出
curl -s "http://127.0.0.1:8080/api/v1/period-stats?minutes=60"
curl -s -o period-stats.csv "http://127.0.0.1:8080/api/v1/export/period-stats.csv?minutes=60"

# 页面
# 打开 http://127.0.0.1:8080/ → 「时段统计」页签 → 切换预设/自定义 → 看集群表与各主机表 → 点选主机看单机趋势 → 导出 CSV
```

无数据时先灌演示点：`python3 scripts/demo_seed.py --url http://127.0.0.1:8080/api/v1/metrics`。

## 存储

`metrics_history` 压缩字段在原有趋势键上**只增不删**：`net_rated_mbps`、`net_rx_percent`、`net_tx_percent`、`mem_total_mb`、`disk_total_gb`。不写入网卡列表，避免撑库。未改 Agent 昇腾解析。
