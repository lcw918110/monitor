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

返回完整最新 `payload` 与 `online` 标志。

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

统计口径：仅对 **在线** 主机计算平均值；GPU 卡数统计全部已知主机最新快照中的卡（含刚离线，便于盘点资产）。卡片总数以最新快照为准。

### 1.5 时段利用 `GET /api/v1/hosts/{id}/period-stats` 与 `GET /api/v1/period-stats`

见 `docs/11-period-utilization.md`。相对窗口 `minutes` 保持兼容；绝对窗口 `from_ts`/`to_ts`（Unix 秒）成对使用并裁剪到保留期。集群汇总为**样本加权**（合并各机窗口内样本后再聚合）。

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

1. **顶部**：产品名 + 刷新时间 + 集群统计卡片
2. **中部**：主机表格（可点击）
3. **右侧/下方**：选中主机详情（系统 + GPU 表）

风格：清晰运维控制台，中文标签，数字突出；避免花哨装饰。

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
