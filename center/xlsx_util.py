"""用标准库读写简易 .xlsx（不依赖 openpyxl）。"""

from __future__ import annotations

import io
import re
import zipfile
from typing import List, Optional, Sequence
from xml.etree import ElementTree as ET


NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_OFFICE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _col_row(cell_ref: str) -> tuple:
    m = re.match(r"([A-Z]+)(\d+)", cell_ref.upper())
    if not m:
        return 0, 0
    col_s, row_s = m.group(1), m.group(2)
    col = 0
    for ch in col_s:
        col = col * 26 + (ord(ch) - ord("A") + 1)
    return col - 1, int(row_s) - 1


def _cell_text(cell: ET.Element, shared: List[str]) -> str:
    cell_type = cell.attrib.get("t")
    v = cell.find("{%s}v" % NS_MAIN)
    is_elem = cell.find("{%s}is" % NS_MAIN)
    if cell_type == "s" and v is not None and v.text is not None:
        idx = int(v.text)
        return shared[idx] if 0 <= idx < len(shared) else ""
    if cell_type == "inlineStr" and is_elem is not None:
        t = is_elem.find(".//{%s}t" % NS_MAIN)
        return t.text if t is not None and t.text else ""
    if v is not None and v.text is not None:
        return v.text
    return ""


def read_xlsx_rows(data: bytes, max_rows: int = 5000) -> List[List[str]]:
    """读取首个工作表，返回二维字符串表（含表头）。"""
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("{%s}si" % NS_MAIN):
                texts = [t.text or "" for t in si.findall(".//{%s}t" % NS_MAIN)]
                shared.append("".join(texts))

        sheet_name = "xl/worksheets/sheet1.xml"
        names = zf.namelist()
        if sheet_name not in names:
            candidates = [n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
            if not candidates:
                raise ValueError("xlsx 中未找到工作表")
            sheet_name = sorted(candidates)[0]

        root = ET.fromstring(zf.read(sheet_name))
        sheet_data = root.find("{%s}sheetData" % NS_MAIN)
        if sheet_data is None:
            return []

        grid: dict = {}
        max_r, max_c = -1, -1
        for row in sheet_data.findall("{%s}row" % NS_MAIN):
            for cell in row.findall("{%s}c" % NS_MAIN):
                ref = cell.attrib.get("r")
                if not ref:
                    continue
                c, r = _col_row(ref)
                if r >= max_rows:
                    continue
                val = _cell_text(cell, shared).strip()
                grid[(r, c)] = val
                max_r = max(max_r, r)
                max_c = max(max_c, c)

        rows: List[List[str]] = []
        for r in range(max_r + 1):
            rows.append([grid.get((r, c), "") for c in range(max_c + 1)])
        return rows


def _col_name(idx: int) -> str:
    # 0 -> A
    n = idx + 1
    s = ""
    while n:
        n, rem = divmod(n - 1, 26)
        s = chr(65 + rem) + s
    return s


def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def build_xlsx(rows: Sequence[Sequence[str]]) -> bytes:
    """生成仅含一个工作表的简易 xlsx。"""
    # shared strings
    shared: List[str] = []
    index = {}
    cells_xml = []
    for r_i, row in enumerate(rows):
        parts = []
        for c_i, val in enumerate(row):
            text = "" if val is None else str(val)
            if text not in index:
                index[text] = len(shared)
                shared.append(text)
            sid = index[text]
            ref = "%s%s" % (_col_name(c_i), r_i + 1)
            parts.append('<c r="%s" t="s"><v>%s</v></c>' % (ref, sid))
        cells_xml.append('<row r="%s">%s</row>' % (r_i + 1, "".join(parts)))

    shared_xml = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<sst xmlns="%s" count="%d" uniqueCount="%d">'
        % (NS_MAIN, len(shared), len(shared)),
    ]
    for s in shared:
        shared_xml.append("<si><t>%s</t></si>" % _xml_escape(s))
    shared_xml.append("</sst>")

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="%s"><sheetData>%s</sheetData></worksheet>'
        % (NS_MAIN, "".join(cells_xml))
    )
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s">'
        "<sheets><sheet name=\"客户端清单\" sheetId=\"1\" r:id=\"rId1\"/></sheets>"
        "</workbook>"
    ) % (NS_MAIN, NS_OFFICE_REL)
    workbook_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="%s/sharedStrings" Target="sharedStrings.xml"/>'
        "</Relationships>"
    ) % (NS_REL, NS_OFFICE_REL, NS_OFFICE_REL)
    root_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s">'
        '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ) % (NS_REL, NS_OFFICE_REL)
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        "</Types>"
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        zf.writestr("xl/sharedStrings.xml", "\n".join(shared_xml))
    return buf.getvalue()


TEMPLATE_HEADERS = [
    "ip",
    "host_id",
    "hostname",
    "host_type",
    "ssh_user",
    "ssh_port",
    "ssh_password",
    "ssh_key_path",
    "remote_dir",
    "peer_hosts",
]


def build_import_template() -> bytes:
    sample = [
        TEMPLATE_HEADERS,
        [
            "10.0.0.11",
            "gpu-01",
            "gpu-01",
            "gpu",
            "root",
            "22",
            "",
            "",
            "/opt/monitor-agent",
            "网关=10.0.0.1;存储=10.0.0.50",
        ],
        [
            "10.0.0.12",
            "gpu-02",
            "gpu-02",
            "auto",
            "ubuntu",
            "22",
            "your-password",
            "",
            "~/monitor-agent",
            "网关=10.0.0.1;业务机=10.0.0.21",
        ],
        [
            "10.0.0.21",
            "cpu-01",
            "cpu-01",
            "cpu",
            "root",
            "22",
            "",
            "/home/ops/.ssh/id_rsa_cpu",
            "/opt/monitor",
            "10.0.0.1,10.0.0.11",
        ],
    ]
    return build_xlsx(sample)


def rows_to_target_dicts(rows: List[List[str]]) -> List[dict]:
    if not rows:
        return []
    header = [h.strip().lower() for h in rows[0]]
    # 兼容中文表头
    mapping_alias = {
        "ip": "ip",
        "地址": "ip",
        "主机ip": "ip",
        "host_id": "host_id",
        "主机id": "host_id",
        "hostname": "hostname",
        "主机名": "hostname",
        "显示名": "hostname",
        "host_type": "host_type",
        "类型": "host_type",
        "ssh_user": "ssh_user",
        "ssh用户": "ssh_user",
        "用户": "ssh_user",
        "ssh_port": "ssh_port",
        "ssh端口": "ssh_port",
        "端口": "ssh_port",
        "ssh_password": "ssh_password",
        "ssh密码": "ssh_password",
        "密码": "ssh_password",
        "ssh_key_path": "ssh_key_path",
        "ssh私钥": "ssh_key_path",
        "私钥路径": "ssh_key_path",
        "remote_dir": "remote_dir",
        "安装目录": "remote_dir",
        "peer_hosts": "peer_hosts",
        "peers": "peer_hosts",
        "指定机器": "peer_hosts",
        "关联机器": "peer_hosts",
        "网络探测目标": "peer_hosts",
    }
    idxs = {}
    for i, h in enumerate(header):
        key = mapping_alias.get(h)
        if key:
            idxs[key] = i
    # 无表头时：按固定列序
    if "ip" not in idxs:
        data_rows = rows
        result = []
        for row in data_rows:
            if not row or not str(row[0]).strip():
                continue
            item = {
                "ip": str(row[0]).strip(),
                "host_id": str(row[1]).strip() if len(row) > 1 and row[1] else str(row[0]).strip(),
                "hostname": str(row[2]).strip() if len(row) > 2 and row[2] else "",
                "host_type": _norm_type(str(row[3]).strip() if len(row) > 3 and row[3] else "auto"),
                "ssh_user": str(row[4]).strip() if len(row) > 4 and row[4] else "",
                "ssh_port": str(row[5]).strip() if len(row) > 5 and row[5] else "",
                "ssh_password": str(row[6]).strip() if len(row) > 6 and row[6] else "",
                "ssh_key_path": str(row[7]).strip() if len(row) > 7 and row[7] else "",
                "remote_dir": str(row[8]).strip() if len(row) > 8 and row[8] else "",
                "peer_hosts": str(row[9]).strip() if len(row) > 9 and row[9] else "",
            }
            result.append(item)
        return result

    result = []
    for row in rows[1:]:
        if not row:
            continue

        def get(k: str, default: str = "") -> str:
            i = idxs.get(k)
            if i is None or i >= len(row):
                return default
            return str(row[i]).strip()

        ip = get("ip")
        if not ip:
            continue
        result.append(
            {
                "ip": ip,
                "host_id": get("host_id") or ip,
                "hostname": get("hostname") or get("host_id") or ip,
                "host_type": _norm_type(get("host_type") or "auto"),
                "ssh_user": get("ssh_user"),
                "ssh_port": get("ssh_port"),
                "ssh_password": get("ssh_password"),
                "ssh_key_path": get("ssh_key_path"),
                "remote_dir": get("remote_dir"),
                "peer_hosts": get("peer_hosts"),
            }
        )
    return result


def _norm_type(v: str) -> str:
    from common.host_type import normalize_host_type

    return normalize_host_type(v)
