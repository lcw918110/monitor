# 多挂载点磁盘指标

Agent 采集本机**本地真实文件系统**的多个挂载点，中心端列表只展示一条汇总，详情再展开分盘。

## 报文

`system` 仍保留旧字段，并增加列表：

```json
{
  "disk_total_gb": 500.0,
  "disk_used_gb": 380.0,
  "disk_percent": 90.0,
  "disk_count": 2,
  "disks": [
    {
      "mount": "/data",
      "device": "/dev/sdb1",
      "fstype": "xfs",
      "total_gb": 400.0,
      "used_gb": 360.0,
      "percent": 90.0
    },
    {
      "mount": "/",
      "device": "/dev/sda1",
      "fstype": "ext4",
      "total_gb": 100.0,
      "used_gb": 20.0,
      "percent": 20.0
    }
  ]
}
```

旧 Agent 只报 `disk_*`、没有 `disks[]` 时，中心与页面按单盘回退，行为与以前一致。

## 汇总口径

| 字段 | 规则 |
| --- | --- |
| `disk_total_gb` / `disk_used_gb` | 纳入列表的挂载点**容量求和**（看整机还剩多少空间） |
| `disk_percent` | 各挂载点利用率的**最大值**（告警看最满的那块，避免被大盘摊薄） |
| `disk_count` | `disks` 条数 |
| `disks[]` | 按利用率降序，其次按挂载路径 |

单盘时三个数字仍互相匹配，与旧版 `os.statvfs(disk_path)` 一致。

异常判定使用上述 `disk_percent`（或 `disks[]` 中的更大值），文案带上最满挂载路径，例如 `磁盘 /data 使用偏高：88.0%`。时段统计 / 历史趋势仍只用汇总 `disk_percent`，不把分盘列表写入 `metrics_history`。

## 挂载过滤

来源：Linux `/proc/mounts`（失败则 `df -kP`）。`agent.json` 的 `disk_path`（默认 `/`）**始终计入**。

会丢掉：

- 虚拟/伪文件系统：`tmpfs`、`overlay`、`proc`、`sysfs`、`devtmpfs`、`squashfs` 等
- 网络盘：`nfs*`、`cifs`/`smb*` 等
- 容器/快照路径：`/var/lib/docker`、`/var/lib/containers`、`/snap`、`/boot/efi` 等
- 多数 `fuse.*`（保留 `fuseblk` / NTFS / exFAT）
- 容量 **小于 1 GiB** 的分区（主路径除外）
- 同一块设备的 bind / 同池子卷：只留主路径或最短挂载点，避免 `/` 与 bind 重复计数

## 界面

- **主机列表**：一列磁盘汇总百分比；多于一块时附带 `N盘`
- **主机详情**：一行合计 + 最满挂载提示；两块及以上才出现紧凑分盘表（挂载 | 已用/总量 | %）
