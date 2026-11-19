#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sheet_writer.py —— 解析模板结构与样式，生成 worksheet XML。

分两步：
1) TemplateInfo.from_template(path, cfg) 读取模板 sheet 的表头、样式模板、
   列宽、合并单元格、冻结窗格，并生成「目标工作簿样式模板」。
2) build_sheet_xml(info, rows, target_styles_ctx, cfg, existing_shared_len) 生成
   新的 <worksheet> XML 字符串。

样式保真策略：新 sheet 的单元格样式引用「目标工作簿」的 cellXfs 索引。
调用方需用 styles_bridge.bridge_styles() 把模板样式合并进目标 styles.xml，
得到 模板样式索引 -> 目标样式索引 的映射，再传给本模块。
"""
from __future__ import annotations

import re
from datetime import date, datetime
from xml.etree import ElementTree as ET

from xlsxzip import NS_MAIN, NS_REL_DOC, _prefix

NS_EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
ET.register_namespace("", NS_MAIN)
ET.register_namespace("r", NS_REL_DOC)


def col_letter(idx: int) -> str:
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


class TemplateInfo:
    def __init__(self):
        self.headers = []            # [(col_idx, name)]
        self.header_cells = {}       # col_idx -> {'s': style_idx}  模板表头样式
        self.row_style = {}          # col_idx -> style_idx (数据行样式模板)
        self.merged = []             # ['A1:B1', ...]
        self.freeze = None           # 'A2' or None
        self.col_widths = {}         # 'A' -> width
        self.row_heights = {}        # row_num -> height
        self.data_start_row = 2
        self.total_header_rows = 1
        self.sheet_xml_raw = None

    @classmethod
    def from_template(cls, path: str, cfg: dict):
        import openpyxl
        tcfg = cfg["template"]
        wb = openpyxl.load_workbook(path, data_only=False)
        try:
            idx = tcfg.get("sheet_index", 0)
            name = tcfg.get("sheet_name_in_template")
            if name and name in wb.sheetnames:
                ws = wb[name]
            else:
                ws = wb.worksheets[min(int(idx), len(wb.worksheets) - 1)]
            info = cls()
            hr = int(tcfg.get("header_row", 1))
            info.data_start_row = int(tcfg.get("data_start_row", hr + 1))
            style_row = int(tcfg.get("style_from_row", hr + 1))

            for c in range(1, ws.max_column + 1):
                v = ws.cell(row=hr, column=c).value
                if v is not None:
                    info.headers.append((c, str(v).strip()))
                hc = ws.cell(row=hr, column=c)
                info.header_cells[c] = {"s": hc.style_id or 0}
                sc = ws.cell(row=min(style_row, ws.max_row), column=c)
                info.row_style[c] = sc.style_id or 0

            if tcfg.get("copy_merged_cells", True):
                info.merged = [str(m) for m in ws.merged_cells.ranges]
            if tcfg.get("copy_freeze_panes", True) and ws.freeze_panes:
                info.freeze = str(ws.freeze_panes)
            if tcfg.get("copy_column_widths", True):
                for k, dim in ws.column_dimensions.items():
                    if dim.width:
                        info.col_widths[k] = dim.width
            if tcfg.get("copy_row_heights", False):
                for r, dim in ws.row_dimensions.items():
                    if dim.height:
                        info.row_heights[str(r)] = dim.height
            info.total_header_rows = info.data_start_row - 1
            return info
        finally:
            wb.close()


def _cell_value_xml(col: int, row: int, value, style_idx: int,
                    shared_strings: dict, inline_strings: bool = True) -> str:
    ref = "%s%d" % (col_letter(col), row)
    s_attr = ' s="%d"' % style_idx if style_idx else ""
    if value is None or value == "":
        return '<c r="%s"%s/>' % (ref, s_attr)
    if isinstance(value, bool):
        return '<c r="%s"%s t="b"><v>%d</v></c>' % (ref, s_attr, 1 if value else 0)
    if isinstance(value, (int, float)):
        return '<c r="%s"%s><v>%s</v></c>' % (ref, s_attr, value)
    if isinstance(value, (date, datetime)):
        serial = _excel_serial(value)
        return '<c r="%s"%s><v>%s</v></c>' % (ref, s_attr, serial)
    text = str(value)
    text = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    if inline_strings:
        return '<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, s_attr, text)
    # shared string
    if text not in shared_strings:
        shared_strings[text] = len(shared_strings)
    return '<c r="%s"%s t="s"><v>%d</v></c>' % (ref, s_attr, shared_strings[text])


def _excel_serial(v) -> float:
    from datetime import date as _d, datetime as _dt
    base = _d(1899, 12, 30)
    if isinstance(v, _dt):
        delta = v - _dt(1899, 12, 30)
        return delta.days + delta.seconds / 86400.0 + delta.microseconds / 86400000000.0
    return (v - base).days


def build_sheet_xml(info: TemplateInfo, rows: list, style_map: dict,
                    cfg: dict, include_header: bool = True) -> str:
    """生成 worksheet XML。

    rows: list of list，每个内层 list 长度=模板列数，元素为值或 None。
    style_map: dict  模板cellXfs索引 -> 目标cellXfs索引（由 styles_bridge 提供）。
    """
    parts = []
    parts.append('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    parts.append('<worksheet xmlns="%s" xmlns:r="%s">' % (NS_MAIN, NS_REL_DOC))

    # sheetViews / freeze
    if info.freeze:
        parts.append('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="%s" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' % info.freeze)
    else:
        parts.append('<sheetViews><sheetView workbookViewId="0"/></sheetViews>')

    parts.append('<sheetFormatPr defaultRowHeight="15"/>')

    if info.col_widths:
        parts.append('<cols>')
        for col in sorted(info.col_widths, key=lambda c: _col_to_num(c)):
            parts.append('<col min="%d" max="%d" width="%s" customWidth="1"/>' %
                         (_col_to_num(col), _col_to_num(col), info.col_widths[col]))
        parts.append('</cols>')

    parts.append('<sheetData>')
    if include_header:
        hr = 1
        parts.append('<row r="%d">' % hr)
        for c, name in info.headers:
            s_tpl = info.header_cells.get(c, {}).get("s", 0)
            s = style_map.get(s_tpl, 0)
            parts.append(_cell_value_xml(c, hr, name, s, {}))
        parts.append('</row>')

    data_start = max(info.data_start_row, 2)
    max_cols = max((c for c, _ in info.headers), default=1)
    for ri, row in enumerate(rows):
        rnum = data_start + ri
        parts.append('<row r="%d">' % rnum)
        for ci in range(1, max_cols + 1):
            val = row[ci - 1] if ci - 1 < len(row) else None
            s_tpl = info.row_style.get(ci, 0)
            s = style_map.get(s_tpl, 0)
            parts.append(_cell_value_xml(ci, rnum, val, s, {}))
        parts.append('</row>')
    parts.append('</sheetData>')

    if info.merged:
        parts.append('<mergeCells count="%d">' % len(info.merged))
        for m in info.merged:
            parts.append('<mergeCell ref="%s"/>' % m)
        parts.append('</mergeCells>')

    # 数据验证/条件格式等由模板带来的不做复制（避免样式索引错乱），保持最小安全集
    parts.append('<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>')
    parts.append('</worksheet>')
    return "".join(parts)


def _col_to_num(col: str) -> int:
    n = 0
    for ch in col.upper():
        n = n * 26 + (ord(ch) - 64)
    return n
