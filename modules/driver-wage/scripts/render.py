#!/usr/bin/env python3
"""统一表格渲染 + 逐司机工资条。

「文字 / 背景 / 字体 / 颜色 / 模板统一」由 config/style.json 单一来源控制：
  * extract-style：从发起人指定的模板 xlsx 抽取字体、字号、粗体、底色、边框、
    对齐、列宽，生成 style.json；之后所有产出都套同一份规范。
  * render：所有 sheet、所有司机、所有月份共用该规范，不再各表各异。
  * 每位司机一份独立工资条文件 + 一条纯文本消息，只含本人数据。
"""
from __future__ import annotations

import json
import os
from decimal import Decimal

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from config import ConfigError


def _rgb(v, default="FFFFFFFF"):
    v = str(v or default).replace("#", "").upper()
    return v if len(v) == 8 else ("FF" + v if len(v) == 6 else default)


def _font(st, kind="data"):
    f = st.get("font", {})
    part = st.get(kind, {})
    return Font(name=f.get("name", "PingFang SC"), size=f.get("size", 11),
                bold=part.get("bold", False),
                color=_rgb(part.get("fg") or f.get("color"), "FF000000"))


def _fill(color):
    return PatternFill("solid", fgColor=_rgb(color))


def _border(st):
    c = _rgb(st.get("palette", {}).get("border"), "FFBFBFBF")
    side = Side(style="thin", color=c)
    return Border(left=side, right=side, top=side, bottom=side)


def _align(st, kind="data"):
    return Alignment(horizontal=st.get(kind, {}).get("align", "center"),
                     vertical="center", wrap_text=False)


def _write_table(ws, st, headers, rows, money_cols=(), int_cols=(),
                 total_row=None, title=None):
    """写一张统一样式的表：可选标题行 + 表头 + 数据 + 合计。"""
    pal = st.get("palette", {})
    nf = st.get("number_format", {})
    r = 1
    if title:
        ws.cell(1, 1, title)
        ws.cell(1, 1).font = _font(st, "title")
        ws.cell(1, 1).fill = _fill(pal.get("title_bg", "FF70AD47"))
        ws.cell(1, 1).alignment = Alignment(horizontal="center", vertical="center")
        ws.merge_cells(start_row=1, start_column=1, end_row=1,
                       end_column=max(1, len(headers)))
        ws.row_dimensions[1].height = st.get("header", {}).get("height", 24) + 4
        r = 2
    header_row = r
    for c, h in enumerate(headers, 1):
        cell = ws.cell(r, c, h)
        cell.font = _font(st, "header")
        cell.fill = _fill(pal.get("header_bg", "FFFFE699"))
        cell.alignment = _align(st, "header")
        cell.border = _border(st)
    ws.row_dimensions[r].height = st.get("header", {}).get("height", 24)
    for row in rows:
        r += 1
        for c, v in enumerate(row, 1):
            cell = ws.cell(r, c, v)
            cell.font = _font(st, "data")
            cell.fill = _fill(pal.get("data_bg", "FFFFFFFF"))
            cell.alignment = _align(st, "data")
            cell.border = _border(st)
            if c in money_cols and isinstance(v, (int, float, Decimal)):
                cell.number_format = nf.get("money", "¥#,##0.00")
            elif c in int_cols and isinstance(v, (int, float, Decimal)):
                cell.number_format = nf.get("km", "#,##0")
        ws.row_dimensions[r].height = st.get("data", {}).get("height", 20)
    if total_row:
        r += 1
        for c, v in enumerate(total_row, 1):
            cell = ws.cell(r, c, v)
            cell.font = _font(st, "total")
            cell.fill = _fill(pal.get("total_bg", "FFFFE699"))
            cell.alignment = _align(st, "total")
            cell.border = _border(st)
            if c in money_cols and isinstance(v, (int, float, Decimal)):
                cell.number_format = nf.get("money", "¥#,##0.00")
        ws.row_dimensions[r].height = st.get("header", {}).get("height", 24)
    for i, h in enumerate(headers, 1):
        width = st.get("columns", {}).get(h, st.get("default_width", 14))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = f"A{header_row + 1}"
    if header_row and st.get("auto_filter", True):
        ws.auto_filter.ref = (f"A{header_row}:"
                              f"{get_column_letter(len(headers))}{r}")
    return r


def render_workbook(cfg: dict, month: str, result: dict, verification: dict) -> str:
    st = cfg["style"]
    names = st.get("sheet_names", {})
    out_dir = os.path.join(cfg["paths"]["out_dir"], month)
    os.makedirs(out_dir, exist_ok=True)
    wb = Workbook()

    ws = wb.active
    ws.title = names.get("summary", "工资汇总")
    headers = ["司机", "月份", "趟数", "公里数", "应发工资", "报销", "扣款", "实发工资", "规则状态"]
    rows = [[s["display_name"], month, s["trip_count"], float(s["km_total"]),
             float(s["gross"]), float(s["reimbursement"]),
             float(s["deduction_total"]), float(s["net"]),
             "已确认" if s["rule_confirmed"] else "未确认"] for s in result["payslips"]]
    total = ["合计", month, result["totals"]["trips"], float(result["totals"]["km"]),
             float(result["totals"]["gross"]), float(result["totals"]["reimbursement"]),
             float(result["totals"]["deduction_total"]), float(result["totals"]["net"]),
             verification["verdict"]]
    _write_table(ws, st, headers, rows, money_cols=(5, 6, 7, 8), int_cols=(3, 4),
                 total_row=total, title=f"{month} 司机工资汇总表（核对结论：{verification['verdict']}）")

    wd = wb.create_sheet(names.get("detail", "工资明细"))
    dheaders = ["司机", "日期", "业务", "车牌", "趟次", "路线", "公里数", "工资", "来源", "行号"]
    drows = []
    for s in result["payslips"]:
        for l in s["lines"]:
            drows.append([s["display_name"], l.get("date", ""), "", l.get("plate", ""),
                          l.get("trip_no", ""), l.get("route", ""),
                          float(l.get("km") or 0),
                          float(l["wage"]) if l.get("wage") is not None else l.get("error", ""),
                          l.get("source", ""), l.get("source_row", "")])
    _write_table(wd, st, dheaders, drows, money_cols=(8,), int_cols=(7,),
                 title=f"{month} 司机工资逐行明细")

    path = os.path.join(out_dir, f"工资表_{month}.xlsx")
    wb.save(path)
    return path


def render_payslips(cfg: dict, month: str, result: dict) -> list[dict]:
    """每人一份独立 xlsx + 一条只含本人的消息文本。"""
    st = cfg["style"]
    out_dir = os.path.join(cfg["paths"]["out_dir"], month, "payslips")
    os.makedirs(out_dir, exist_ok=True)
    tpl = cfg["payroll"].get("notify", {}).get("message_template") or (
        "【工资条｜{month}】{name}\n"
        "应发 ¥{gross}（{trips} 趟 / {km} 公里）\n"
        "报销 ¥{reimbursement}　扣款 ¥{deductions}\n"
        "实发 ¥{net}\n"
        "明细见附件，如有疑问请回复核对。")
    made = []
    for s in result["payslips"]:
        safe = "".join(ch for ch in s["driver"] if ch.isalnum() or ch in "-_") or "driver"
        wb = Workbook()
        ws = wb.active
        ws.title = st.get("sheet_names", {}).get("payslip", "工资条")
        headers = ["日期", "路线", "公里数", "趟次", "工资", "来源", "行号"]
        rows = [[l.get("date", ""), l.get("route", ""), float(l.get("km") or 0),
                 l.get("trip_no", ""),
                 float(l["wage"]) if l.get("wage") is not None else l.get("error", ""),
                 l.get("source", ""), l.get("source_row", "")] for l in s["lines"]]
        total = ["合计", "", float(s["km_total"]), s["trip_count"],
                 float(s["net"]), "", ""]
        _write_table(ws, st, headers, rows, money_cols=(5,), int_cols=(3,),
                     total_row=total,
                     title=f"{s['display_name']} · {month} 工资条（实发 ¥{s['net']}）")
        xlsx = os.path.join(out_dir, f"{safe}_{month}.xlsx")
        wb.save(xlsx)

        text = tpl.format(month=month, name=s["display_name"], gross=s["gross"],
                          trips=s["trip_count"], km=s["km_total"],
                          reimbursement=s["reimbursement"],
                          deductions=s["deduction_total"], net=s["net"])
        txt = os.path.join(out_dir, f"{safe}_{month}.txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        made.append({"driver": s["driver"], "display_name": s["display_name"],
                     "xlsx": xlsx, "text_path": txt, "text": text,
                     "result_hash": result["result_hash"]})
    return made


def extract_style(template_path: str, out_path: str, header_row: int = 1,
                  data_row: int = 2, total_row: int | None = None) -> dict:
    """从发起人指定的模板 xlsx 抽取样式，生成统一 style.json。只读模板。"""
    if not os.path.isfile(template_path):
        raise ConfigError(f"模板不存在: {template_path}")
    wb = load_workbook(template_path, data_only=False)
    ws = wb[wb.sheetnames[0]]

    def rgb_of(x, default=None):
        try:
            v = str(x.rgb) if x is not None and isinstance(x.rgb, str) else default
        except Exception:
            v = default
        # 无填充在 openpyxl 里是 00000000；统一模板要求数据区白底，不能留空
        return default if not v or v == "00000000" else v

    h = ws.cell(header_row, 1)
    d = ws.cell(data_row, 1)
    t = ws.cell(total_row, 1) if total_row else h
    style = {
        "version": 1,
        "_source_template": os.path.basename(template_path),
        "font": {"name": h.font.name or "PingFang SC",
                 "size": h.font.sz or 11,
                 "color": rgb_of(h.font.color, "FF000000")},
        "palette": {
            "header_bg": rgb_of(h.fill.fgColor, "FFFFE699"),
            "header_fg": rgb_of(h.font.color, "FF000000"),
            "data_bg": rgb_of(d.fill.fgColor, "FFFFFFFF"),
            "total_bg": rgb_of(t.fill.fgColor, "FFFFE699"),
            "title_bg": "FF70AD47", "title_fg": "FFFFFFFF",
            "border": "FFBFBFBF",
            "accent_green": "FF70AD47", "accent_orange": "FFED7D31",
        },
        "header": {"bold": bool(h.font.b), "align": h.alignment.horizontal or "center",
                   "height": 24},
        "data": {"align": d.alignment.horizontal or "center", "height": 20},
        "total": {"bold": bool(t.font.b), "align": t.alignment.horizontal or "center"},
        "title": {"bold": True, "align": "center", "fg": "FFFFFFFF"},
        "number_format": {"money": "¥#,##0.00", "km": "#,##0",
                          "date": "yyyy-mm-dd", "integer": "0"},
        "columns": {str(ws.cell(header_row, c).value or get_column_letter(c)):
                    round(ws.column_dimensions[get_column_letter(c)].width or 14, 1)
                    for c in range(1, min(ws.max_column, 12) + 1)},
        "default_width": 14, "auto_filter": True,
        "sheet_names": {"summary": "工资汇总", "detail": "工资明细", "payslip": "工资条"},
    }
    wb.close()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(style, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return style
