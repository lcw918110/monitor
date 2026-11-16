"""给人看的离线导出：中文列名、含义和单位。

hosts.json 仍用英文键，供程序读取。CSV 是给人打开的文件，表头用中文。
数值本身不在这里改写；空值写成空单元格，不补 0。
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterable, List, NamedTuple, Sequence

from center.storage import PERIOD_METRIC_KEYS


class ExportColumn(NamedTuple):
    key: str
    header: str
    meaning: str
    unit: str


def _cell(value: Any) -> Any:
    if value is None:
        return ""
    return value


def write_labeled_csv(rows: Iterable[Dict[str, Any]], columns: Sequence[ExportColumn]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([col.header for col in columns])
    for row in rows:
        writer.writerow([_cell(row.get(col.key)) for col in columns])
    return buf.getvalue()


def field_guide_csv(columns: Sequence[ExportColumn]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["字段", "含义", "单位"])
    for col in columns:
        writer.writerow([col.header, col.meaning, col.unit or "无"])
    return buf.getvalue()


HOST_COLUMNS: List[ExportColumn] = [
    ExportColumn("host_id", "主机标识", "中心内稳定的主机编号。JSON 键 host_id", "无"),
    ExportColumn("hostname", "主机名", "操作系统主机名。JSON 键 hostname", "无"),
    ExportColumn("address", "展示地址", "页面上的地址：手工地址、未停用的部署清单或 host_id 中的 IP", "无"),
    ExportColumn("group_name", "资源组", "中心侧资源组名称，未分组为空", "无"),
    ExportColumn("host_type", "主机类型", "cpu 或 gpu。gpu 表示带加速卡", "无"),
    ExportColumn("online", "是否在线", "True 为在线，False 为离线。判定与页面相同", "无"),
    ExportColumn("last_seen", "最近上报（Unix秒）", "最后一次上报的 Unix 时间戳", "Unix秒"),
    ExportColumn("cpu_percent", "CPU利用率（%）", "当前 CPU 利用率", "%"),
    ExportColumn("cpu_count", "CPU核数", "逻辑核数", "核"),
    ExportColumn("mem_percent", "内存利用率（%）", "内存已用占总量的百分比", "%"),
    ExportColumn("disk_percent", "磁盘占用率（%）", "各挂载容量占用率的最大值。这是空间，不是读写忙闲", "%"),
    ExportColumn("disk_used_gb", "磁盘已用（GB）", "纳入统计的挂载已用容量之和", "GB"),
    ExportColumn("disk_total_gb", "磁盘总量（GB）", "纳入统计的挂载总容量之和", "GB"),
    ExportColumn("disk_count", "磁盘个数", "纳入统计的挂载条数", "个"),
    ExportColumn(
        "disk_read_mbps",
        "磁盘读取速率（Mbps）",
        "整机整盘读取速率之和。兆比特每秒，与网络接收速率同一单位（扇区按 512 字节，再 ×8÷10^6）。不是 iostat 的 MB/s。刚启动尚无上一笔时为空",
        "Mbps",
    ),
    ExportColumn(
        "disk_write_mbps",
        "磁盘写入速率（Mbps）",
        "整机整盘写入速率之和。单位同磁盘读取速率。刚启动尚无上一笔时为空",
        "Mbps",
    ),
    ExportColumn(
        "disk_io_util_percent",
        "磁盘I/O利用率（%）",
        "纳入整盘里最忙一块的设备忙闲百分比，类似 iostat 的 %util，限制在 0～100。不是容量占用率，也不是按额定带宽算出来的",
        "%",
    ),
    ExportColumn("load1", "1分钟负载", "操作系统 1 分钟负载。读不到为空，不是 0", "无"),
    ExportColumn("npu_count", "NPU数量", "兼容字段：npus 条数", "张"),
    ExportColumn("npu_util_avg", "NPU利用率（%）", "兼容字段：npus 利用率平均", "%"),
    ExportColumn("gpu_count", "GPU数量", "兼容字段：gpus 条数", "张"),
    ExportColumn("gpu_util_avg", "GPU利用率（%）", "兼容字段：gpus 利用率平均", "%"),
    ExportColumn("accel_count", "加速卡数量", "合并后的加速卡张数", "张"),
    ExportColumn("accel_summary", "加速卡型号", "厂商、型号和数量的一行摘要", "无"),
    ExportColumn("net_rx_mbps", "网络接收速率（Mbps）", "非回环网卡接收速率合计，兆比特每秒", "Mbps"),
    ExportColumn("net_tx_mbps", "网络发送速率（Mbps）", "非回环网卡发送速率合计，兆比特每秒", "Mbps"),
    ExportColumn("net_rated_mbps", "额定带宽（Mbps）", "链路额定速率合计。没有额定时空着", "Mbps"),
    ExportColumn("owner", "负责人", "手工资产里的负责人", "无"),
    ExportColumn("purpose", "用途", "手工资产里的用途", "无"),
    ExportColumn("asset_name", "资产名称", "手工资产名称", "无"),
    ExportColumn("asset_cpu", "资产CPU", "手工填写的 CPU 说明", "无"),
    ExportColumn("asset_memory", "资产内存", "手工填写的内存说明", "无"),
    ExportColumn("asset_disk", "资产磁盘", "手工填写的磁盘说明", "无"),
    ExportColumn("asset_gpu", "资产GPU", "手工填写的 GPU 说明", "无"),
    ExportColumn("asset_model", "资产型号", "手工填写的型号或资产编号", "无"),
    ExportColumn("asset_notes", "资产备注", "手工备注", "无"),
]


_PERIOD_METRIC_TEXT = {
    "cpu_percent": ("CPU利用率", "%", "CPU 利用率"),
    "mem_percent": ("内存利用率", "%", "内存利用率"),
    "disk_percent": ("磁盘占用率", "%", "容量占用率的最大值，不是磁盘读写"),
    "disk_read_mbps": ("磁盘读取速率", "Mbps", "整盘读取速率之和，兆比特每秒"),
    "disk_write_mbps": ("磁盘写入速率", "Mbps", "整盘写入速率之和，兆比特每秒"),
    "disk_io_util_percent": (
        "磁盘I/O利用率",
        "%",
        "最忙整盘的设备忙闲百分比，类似 iostat %util，0～100",
    ),
    "load1": ("1分钟负载", "", "1 分钟负载"),
    "net_rx_mbps": ("网络接收速率", "Mbps", "网络接收速率"),
    "net_tx_mbps": ("网络发送速率", "Mbps", "网络发送速率"),
    "net_rated_mbps": ("额定带宽", "Mbps", "链路额定带宽"),
    "net_rx_percent": ("网络接收占额定", "%", "接收速率相对额定带宽"),
    "net_tx_percent": ("网络发送占额定", "%", "发送速率相对额定带宽"),
    "accel_util_avg": ("加速卡利用率", "%", "加速卡利用率平均"),
    "accel_temp_max": ("加速卡最高温度", "°C", "该点上加速卡温度的最大值"),
}

_PERIOD_STATS = (
    ("avg", "平均", "算术平均"),
    ("min", "最低", "最低值"),
    ("max", "最高", "最高值"),
    ("p95", "P95", "线性插值的 95 分位"),
    ("busy_ratio", "繁忙占比", "不低于繁忙阈值的样本比例，0～1。没有阈值时为空，磁盘读写和磁盘I/O利用率默认没有阈值"),
)


def _period_stat_header(label: str, stat_zh: str, unit: str, stat_key: str) -> str:
    if stat_key == "busy_ratio":
        return "%s繁忙占比（0～1）" % label
    if unit:
        return "%s%s（%s）" % (label, stat_zh, unit)
    return "%s%s" % (label, stat_zh)


def period_columns() -> List[ExportColumn]:
    columns = [
        ExportColumn("scope", "范围", "cluster 为集群汇总行，host 为单机行", "无"),
        ExportColumn("host_id", "主机标识", "单机编号。集群汇总行为 __cluster__", "无"),
        ExportColumn("hostname", "主机名", "主机名。集群汇总行为「集群汇总」", "无"),
        ExportColumn("host_type", "主机类型", "cpu 或 gpu。集群汇总行空着", "无"),
        ExportColumn("accel_count", "加速卡数量", "加速卡张数", "张"),
        ExportColumn("accel_summary", "加速卡型号", "加速卡摘要", "无"),
        ExportColumn("sample_count", "样本数", "这一窗口里纳入统计的历史上报条数", "条"),
        ExportColumn("from_ts", "起始时间（Unix秒）", "实际有样本的起点，Unix 秒", "Unix秒"),
        ExportColumn("to_ts", "结束时间（Unix秒）", "实际有样本的终点，Unix 秒", "Unix秒"),
    ]
    for key in PERIOD_METRIC_KEYS:
        label, unit, meaning = _PERIOD_METRIC_TEXT.get(key, (key, "", key))
        for stat_key, stat_zh, stat_meaning in _PERIOD_STATS:
            header = _period_stat_header(label, stat_zh, unit, stat_key)
            columns.append(
                ExportColumn(
                    "%s_%s" % (key, stat_key),
                    header,
                    "%s的%s" % (meaning, stat_meaning),
                    "0～1" if stat_key == "busy_ratio" else (unit or "无"),
                )
            )
    return columns


RANKING_COLUMNS: List[ExportColumn] = [
    ExportColumn("host_id", "主机标识", "主机编号。JSON 报告里的键仍是 host_id", "无"),
    ExportColumn("address", "展示地址", "生成报告时的展示地址", "无"),
    ExportColumn("hostname", "主机名", "主机名", "无"),
    ExportColumn("group_name", "资源组", "资源组", "无"),
    ExportColumn("owner", "负责人", "手工负责人", "无"),
    ExportColumn("purpose", "用途", "手工作途", "无"),
    ExportColumn("online", "是否在线", "生成时刻是否在线。1 在线，0 离线", "无"),
    ExportColumn(
        "pole",
        "闲忙归类",
        "idle 基本不用，high_load 高负荷，normal 中间，offline 离线，insufficient 样本不足。只看 CPU 和加速卡",
        "无",
    ),
    ExportColumn("pole_reason", "归类说明", "闲忙归类的中文原因", "无"),
    ExportColumn("cpu_avg", "CPU利用率平均（%）", "窗口内 CPU 平均利用率", "%"),
    ExportColumn("cpu_p95", "CPU利用率P95（%）", "窗口内 CPU 的 P95", "%"),
    ExportColumn("cpu_busy_ratio", "CPU繁忙占比（0～1）", "CPU 不低于繁忙阈值的样本比例", "0～1"),
    ExportColumn("cpu_samples", "CPU样本数", "CPU 有效样本数", "条"),
    ExportColumn("accel_count", "加速卡数量", "加速卡张数", "张"),
    ExportColumn("accel_summary", "加速卡型号", "加速卡摘要", "无"),
    ExportColumn("accel_avg", "加速卡利用率平均（%）", "加速卡平均利用率", "%"),
    ExportColumn("accel_p95", "加速卡利用率P95（%）", "加速卡利用率 P95", "%"),
    ExportColumn("accel_busy_ratio", "加速卡繁忙占比（0～1）", "加速卡繁忙样本比例", "0～1"),
    ExportColumn("accel_samples", "加速卡样本数", "加速卡有效样本数", "条"),
    ExportColumn("mem_avg", "内存利用率平均（%）", "内存平均利用率。不参与闲忙", "%"),
    ExportColumn("mem_p95", "内存利用率P95（%）", "内存利用率 P95", "%"),
    ExportColumn("disk_avg", "磁盘占用率平均（%）", "容量占用率平均。不是磁盘读写，不参与闲忙", "%"),
    ExportColumn("disk_p95", "磁盘占用率P95（%）", "容量占用率 P95", "%"),
    ExportColumn("disk_max", "磁盘占用率最高（%）", "窗口内容量占用率最高值", "%"),
    ExportColumn("disk_samples", "磁盘样本数", "磁盘占用率有效样本数", "条"),
    ExportColumn("fullest_mount", "最满挂载", "窗口内占用率最高的挂载路径。没有挂载样本时为空", "无"),
    ExportColumn("fullest_mount_percent", "最满挂载占用率（%）", "该挂载的占用率", "%"),
]


def _section_lines(title: str, columns: Sequence[ExportColumn]) -> List[str]:
    lines = [title, ""]
    for col in columns:
        lines.append("- %s：%s。单位：%s。" % (col.header, col.meaning, col.unit or "无"))
    lines.append("")
    return lines


def field_guide_text() -> str:
    """短的汉字字段说明。不要把这段插进数据 CSV 的表头下面。"""
    lines = [
        "简易多机监控 · 离线导出字段说明",
        "",
        "这些下载是 UTF-8 带 BOM，Excel 可以直接打开中文。格子里的数字和页面、接口一致；读不到就留空，不写成 0，也不补假数。",
        "",
        "hosts.csv、period-stats.csv、诊断排行 CSV 的表头是中文，给人离线看。",
        "hosts.json 给程序用，键名保持英文（host_id、cpu_percent、disk_read_mbps 等），不随中文表头改名。",
        "",
        "磁盘占用率（disk_percent）只表示容量，取纳入挂载里最满的一块。",
        "磁盘读取速率、磁盘写入速率是整盘 I/O 之和，单位 Mbps（兆比特/秒），和网络接收速率一样：扇区按 512 字节，再乘 8 除以 10^6。这不是 iostat 显示的 MB/s（大约差 8 倍）。",
        "磁盘I/O利用率是设备忙着做 I/O 的时间百分比，类似 iostat 的 %util，取整盘中的最大值，限制在 0～100。磁盘没有稳定的额定带宽，所以不算「速率 / 额定」。",
        "采集端刚启动、还没有上一笔计数时，这三项为空。旧的历史上报没有这三项时，时段统计里也是空的，不影响原来的 CPU、内存和磁盘占用。",
        "闲忙（基本不用 / 高负荷）只看 CPU 和加速卡。内存、磁盘占用、磁盘读写都不进入闲忙。没有配置繁忙阈值的列，繁忙占比留空。",
        "",
    ]
    lines.extend(_section_lines("一、主机快照 hosts.csv", HOST_COLUMNS))
    lines.extend(_section_lines("二、时段统计 period-stats.csv", period_columns()))
    lines.extend(_section_lines("三、使用诊断排行 rankings.csv", RANKING_COLUMNS))
    lines.append("结构化的同一份说明也可以单独下载：")
    lines.append("- /api/v1/export/hosts-fields.csv")
    lines.append("- /api/v1/export/period-stats-fields.csv")
    lines.append("- /api/v1/export/rankings-fields.csv")
    lines.append("")
    return "\n".join(lines)
