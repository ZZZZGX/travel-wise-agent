# -*- coding: utf-8 -*-
# Copyright (c) 2026 张广鑫. All rights reserved.
# Licensed under the PolyForm Noncommercial License 1.0.0.
# Commercial use is prohibited without a separate commercial license.
# See LICENSE and COMMERCIAL-LICENSE.md for details.
"""matrix_export.py —— 把价格矩阵导成 CSV / Excel / HTML。**全程零 token。**

## 为什么这里一个模型都不用

「用开源大模型的 excel skill 生成表格」听起来省事，实际是把刚解决的问题
原样搬回来：要让模型写出 600 个价格，就得先把这 600 个数字塞进它的上下文，
再让它一个不错地誊写出来。于是

    输入烧一遍 + 输出烧一遍 + 任何一个数字抄错都是编造票价

一模一样的账，只是换了个输出格式。而且多了一层新风险：模型写出来的
xlsx 结构可能是坏的，你要到打开文件那一刻才知道。

判据很简单：**这个任务有没有唯一正确答案？**
「1297 应该写进 CA3845 行、08-25 列」有唯一答案，且已经在内存里了——
那它就是代码的活。模型该做的是「这张表说明了什么」，不是搬运。

所以这里只用标准库：csv / zipfile / xml。不装 openpyxl 也能出 .xlsx，
在你那个嵌入式 Python 里同样跑得动。
"""

from __future__ import annotations

import csv
import zipfile
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .price_matrix import PriceMatrix, _fmt_price


# --------------------------------------------------------------------------
# CSV
# --------------------------------------------------------------------------

def to_csv(m: PriceMatrix, path: str | Path) -> Path:
    """导出 CSV。

    用 utf-8-sig（带 BOM）——否则 Windows 版 Excel 双击打开就是一片乱码，
    这是中文 CSV 最常见的一个坑。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["航班", "航司", "起飞", "到达"] + [c.label for c in m.columns])
        for r in m.rows:
            w.writerow([r.flight_no or r.key, r.airline, r.departure_time, r.arrival_time]
                       + [_cell_csv(m, r, c) for c in m.columns])
        w.writerow(["当日最低", "", "", ""]
                   + [("" if c.min_price is None else c.min_price) for c in m.columns])
        w.writerow([])
        w.writerow(["说明", "空 = 当天无此航班；FAILED = 当天查询失败（数据缺口，非无航班）"])
    return path


def _cell_csv(m: PriceMatrix, row, col):
    if col.status == "failed":
        return "FAILED"
    v = m.cell(row, col)
    return "" if v is None else v


# --------------------------------------------------------------------------
# XLSX（手写，不依赖 openpyxl）
# --------------------------------------------------------------------------

def _col_letter(idx: int) -> str:
    """1 → A，27 → AA。30 天的矩阵会用到两位列名，别偷懒只处理 A~Z。"""
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def _cell(ref: str, value, style: int = 0) -> str:
    if value is None or value == "":
        return '<c r="%s" s="%d"/>' % (ref, style)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, value)
    return ('<c r="%s" s="%d" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
            % (ref, style, escape(str(value))))


def _sheet_xml(rows: list[list], freeze_col: int = 0, freeze_row: int = 0,
               header_style: int = 1, number_style: int = 2,
               heatmap: tuple[int, int, int, int] | None = None,
               col_widths: list[tuple[int, int, float]] | None = None) -> str:
    """把二维数组渲染成 worksheet XML。

    heatmap=(r1, c1, r2, c2) 时给该区域加三色色阶——30 天的价格曲线用颜色扫
    比用眼睛比数字快得多，这才是导出 Excel 相对 markdown 的真正价值。
    """
    out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">']

    if freeze_col or freeze_row:
        top_left = "%s%d" % (_col_letter(freeze_col + 1), freeze_row + 1)
        out.append('<sheetViews><sheetView workbookViewId="0">'
                   '<pane xSplit="%d" ySplit="%d" topLeftCell="%s" '
                   'activePane="bottomRight" state="frozen"/>'
                   '</sheetView></sheetViews>' % (freeze_col, freeze_row, top_left))

    if col_widths:
        out.append("<cols>")
        for lo, hi, width in col_widths:
            out.append('<col min="%d" max="%d" width="%.1f" customWidth="1"/>' % (lo, hi, width))
        out.append("</cols>")

    out.append("<sheetData>")
    for ri, row in enumerate(rows, start=1):
        cells = []
        for ci, value in enumerate(row, start=1):
            ref = "%s%d" % (_col_letter(ci), ri)
            if ri == 1:
                style = header_style
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                style = number_style
            else:
                style = 0
            cells.append(_cell(ref, value, style))
        out.append('<row r="%d">%s</row>' % (ri, "".join(cells)))
    out.append("</sheetData>")

    if heatmap:
        r1, c1, r2, c2 = heatmap
        sqref = "%s%d:%s%d" % (_col_letter(c1), r1, _col_letter(c2), r2)
        out.append('<conditionalFormatting sqref="%s"><cfRule type="colorScale" priority="1">'
                   '<colorScale><cfvo type="min"/><cfvo type="percentile" val="50"/>'
                   '<cfvo type="max"/><color rgb="FF63BE7B"/><color rgb="FFFFEB84"/>'
                   '<color rgb="FFF8696B"/></colorScale></cfRule></conditionalFormatting>'
                   % sqref)

    out.append("</worksheet>")
    return "".join(out)


_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="\u7b49\u7ebf"/></font>
<font><b/><sz val="11"/><name val="\u7b49\u7ebf"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill>
<fill><patternFill patternType="gray125"/></fill>
<fill><patternFill patternType="solid"><fgColor rgb="FFF2F2F2"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="4">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>
<xf numFmtId="3" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="3" fontId="1" fillId="2" borderId="0" xfId="0" applyNumberFormat="1" applyFont="1" applyFill="1"/>
</cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""

_ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""

_WORKBOOK = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="\u4ef7\u683c\u77e9\u9635" sheetId="1" r:id="rId1"/>
<sheet name="\u6bcf\u822a\u73ed\u8981\u70b9" sheetId="2" r:id="rId2"/></sheets></workbook>"""

_WORKBOOK_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def to_xlsx(m: PriceMatrix, path: str | Path) -> Path:
    """导出 .xlsx：冻结表头 + 价格色阶热力图 + 第二张「每航班要点」表。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # -- Sheet 1：矩阵 --
    # 带上到达时刻：判断两个航班号是不是同一架飞机，靠的就是"同起同落"，
    # 只导出起飞时刻的话这件事没法人工复核。
    head = ["航班", "航司", "起飞", "到达"] + [c.label for c in m.columns]
    rows: list[list] = [head]
    for r in m.rows:
        line = [r.flight_no or r.key, r.airline, r.departure_time, r.arrival_time]
        for c in m.columns:
            if c.status == "failed":
                line.append("查询失败")
            else:
                line.append(m.cell(r, c))
        rows.append(line)
    rows.append(["当日最低", "", "", ""] + [c.min_price for c in m.columns])
    rows.append([])
    rows.append(["空 = 当天无此航班；「查询失败」= 数据缺口，非无航班"])
    rows.append(["生成时间", datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "航线", m.route, "消耗额度", m.api_calls])
    for w in m.warnings:
        rows.append(["注意", w])

    heat = None
    if m.rows:
        heat = (2, 5, 1 + len(m.rows), 4 + len(m.columns))
    sheet1 = _sheet_xml(rows, freeze_col=4, freeze_row=1, heatmap=heat,
                        col_widths=[(1, 1, 14), (2, 2, 16), (3, 4, 8),
                                    (5, 4 + max(1, len(m.columns)), 10)])

    # -- Sheet 2：每航班要点 --
    valid = len(m.valid_columns)
    rows2: list[list] = [["航班", "航司", "起飞", "最低价", "最低出现在",
                          "最高价", "波动", "有价天数", "总有效天数"]]
    for r in m.rows:
        rows2.append([r.flight_no or r.key, r.airline, r.departure_time,
                      r.min_price, r.best_date, r.max_price, r.swing,
                      r.observed, valid])
    sheet2 = _sheet_xml(rows2, freeze_row=1,
                        col_widths=[(1, 1, 12), (2, 2, 16), (3, 9, 12)])

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CONTENT_TYPES)
        z.writestr("_rels/.rels", _ROOT_RELS)
        z.writestr("xl/workbook.xml", _WORKBOOK)
        z.writestr("xl/_rels/workbook.xml.rels", _WORKBOOK_RELS)
        z.writestr("xl/styles.xml", _STYLES)
        z.writestr("xl/worksheets/sheet1.xml", sheet1)
        z.writestr("xl/worksheets/sheet2.xml", sheet2)
    return path


# --------------------------------------------------------------------------
# HTML（自带热力图，双击就能看，适合截图 / 发给别人）
# --------------------------------------------------------------------------

def to_html(m: PriceMatrix, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    prices = [v for r in m.rows for v in r.prices.values()]
    lo, hi = (min(prices), max(prices)) if prices else (0, 1)
    span = (hi - lo) or 1

    def color(v: float) -> str:
        # 绿 → 黄 → 红，与 Excel 色阶同一套配色，两边看到的是同一张图
        t = (v - lo) / span
        if t < 0.5:
            r, g = int(99 + (255 - 99) * t * 2), int(190 + (235 - 190) * t * 2)
            b = int(123 + (132 - 123) * t * 2)
        else:
            u = (t - 0.5) * 2
            r, g, b = int(255 - 7 * u), int(235 - 130 * u), int(132 - 25 * u)
        return "rgb(%d,%d,%d)" % (r, g, b)

    cells = []
    for r in m.rows:
        tds = ['<th class="rowhead">%s<span>%s · %s</span></th>'
               % (escape(r.flight_no or r.key), escape(r.airline), escape(r.departure_time))]
        for c in m.columns:
            if c.status == "failed":
                tds.append('<td class="failed" title="当天查询失败，不是没有航班">×</td>')
                continue
            v = m.cell(r, c)
            if v is None:
                tds.append('<td class="none" title="当天无此航班">—</td>')
            else:
                best = " best" if r.best_date == c.day.isoformat() else ""
                tds.append('<td class="p%s" style="background:%s">%s</td>'
                           % (best, color(v), _fmt_price(v)))
        cells.append("<tr>%s</tr>" % "".join(tds))

    head = "".join('<th>%s</th>' % escape(c.label) for c in m.columns)
    foot = "".join('<td class="min">%s</td>'
                   % ("×" if c.status == "failed"
                      else ("—" if c.min_price is None else _fmt_price(c.min_price)))
                   for c in m.columns)
    warn = "".join("<li>%s</li>" % escape(w) for w in m.warnings)

    html = """<!doctype html><html lang="zh"><meta charset="utf-8">
<title>%(route)s 价格矩阵</title><style>
body{font:14px/1.6 system-ui,"Microsoft YaHei",sans-serif;margin:24px;color:#222}
h1{font-size:18px;margin:0 0 4px}p.meta{color:#666;margin:0 0 16px;font-size:13px}
table{border-collapse:collapse}th,td{padding:6px 8px;text-align:center;font-variant-numeric:tabular-nums}
thead th{position:sticky;top:0;background:#fff;border-bottom:2px solid #333;font-weight:600;white-space:nowrap}
th.rowhead{position:sticky;left:0;background:#fff;text-align:left;border-right:2px solid #333;white-space:nowrap}
th.rowhead span{display:block;color:#888;font-weight:400;font-size:12px}
td.p{min-width:56px}td.best{outline:2px solid #111;font-weight:700}
td.none{color:#bbb}td.failed{color:#c00;background:repeating-linear-gradient(45deg,#fff,#fff 4px,#fdd 4px,#fdd 8px)}
tfoot td,tfoot th{border-top:2px solid #333;font-weight:600}
ul.warn{background:#fff8e1;border-left:4px solid #f0b400;padding:10px 10px 10px 28px;max-width:900px}
</style><h1>%(route)s ｜ 未来 %(days)d 天 · 每航班价格矩阵</h1>
<p class="meta">%(range)s ｜ 有效 %(valid)d 天 / 失败 %(failed)d 天 ｜ 航班 %(flights)d 个 ｜
消耗 %(calls)d 次额度 ｜ 生成于 %(now)s<br>
颜色越绿越便宜；粗框 = 该航班自己的最低点；— 当天无此航班；× 当天查询失败（数据缺口，非无航班）</p>
%(warnblock)s
<table><thead><tr><th class="rowhead">航班</th>%(head)s</tr></thead>
<tbody>%(body)s</tbody>
<tfoot><tr><th class="rowhead">当日最低</th>%(foot)s</tr></tfoot></table>
<p class="meta">票价为数据源返回的该航班当日最低可售价，随舱位实时变动；本表是一次快照，不是预测。</p>
</html>""" % {
        "route": escape(m.route), "days": len(m.columns),
        "range": "%s ~ %s" % (m.columns[0].day.isoformat(),
                              m.columns[-1].day.isoformat()) if m.columns else "-",
        "valid": len(m.valid_columns), "failed": len(m.failed_days),
        "flights": len(m.rows), "calls": m.api_calls,
        "now": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "warnblock": ('<ul class="warn">%s</ul>' % warn) if warn else "",
        "head": head, "body": "".join(cells), "foot": foot,
    }
    path.write_text(html, encoding="utf-8")
    return path


_EXPORTERS = {"csv": to_csv, "xlsx": to_xlsx, "excel": to_xlsx, "html": to_html}


def export(m: PriceMatrix, fmt: str, path: str | Path) -> Path:
    fn = _EXPORTERS.get(str(fmt).lower())
    if fn is None:
        raise ValueError("不支持的导出格式「%s」，可选：csv / xlsx / html" % fmt)
    return fn(m, path)


def default_path(m: PriceMatrix, fmt: str, out_dir: str | Path) -> Path:
    """文件名用英文 + 航线码，避免中文文件名在某些环境下的编码问题。"""
    ext = "xlsx" if str(fmt).lower() in ("xlsx", "excel") else str(fmt).lower()
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return Path(out_dir) / ("price-matrix-%s.%s" % (stamp, ext))
