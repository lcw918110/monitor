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

异常判定使用上述 `disk_percent`（或 `disks[]` 中的更大值），文案带上最满挂载路径，例如 `磁盘 /data 使用偏高：88.0%`。时段统计的容量 avg/P95 仍只用汇总 `disk_percent`。`metrics_history` 会额外留下精简挂载（`mount`、`percent`、`used_gb`、`total_gb`，不含 device/fstype），供使用诊断报告在对应时间窗里指出最满挂载；旧数据没有该字段时报告写「挂载样本不足」。

## 磁盘读写（与容量分开）

容量看挂载还剩多少空间。读写看整块盘忙不忙，两套数字不互相换算，也不进入闲忙（闲忙只看 CPU 和加速卡）。磁盘读写也不参与在线 / 「离线」判定。

| 字段 | 规则 |
| --- | --- |
| `disk_read_mbps` / `disk_write_mbps` | 纳入整盘的读取、写入速率之和。单位 Mbps，与 `net_rx_mbps` 相同：`/proc/diskstats` 的扇区按 512 字节，再 ×8÷10^6。不是 iostat 的 MB/s |
| `disk_io_util_percent` | 纳入整盘里设备忙闲百分比的最大值，类似 iostat `%util`，限制在 0～100。磁盘没有稳定额定带宽，不计算「速率 / 额定」 |
| `disk_io[]` | 可选明细：`name`、`read_mbps`、`write_mbps`、`util_percent`。历史里最多留 16 条 |

采集读 Linux `/proc/diskstats`，和上一笔快照做差。只计整盘：`sda`、`nvme0n1`、`vda`、`xvda`、`mmcblk0`。不计 `sda1`、`nvme0n1p1` 这类分区，以免和整盘加两次。跳过 `loop`、`ram`、`zram`、`sr`、`dm-`、`md` 等。某一行解析失败就丢掉该行，其余照常。进程刚启动还没有上一笔时，这三项为 null，页面显示 `-`。计数回绕时该盘这一笔不算，不编造一个很大的速率。旧 Agent 或旧历史点没有这些字段时，时段统计里对应样本为空，不影响 `disk_percent`。

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
- **主机详情**：在「硬件基本信息」页签。一行合计 + 最满挂载提示；两块及以上才出现紧凑分盘列表（挂载、已用/总量、%）
