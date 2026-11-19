#!/usr/bin/env python3
"""从历史工资表反推候选计薪规则（一次性 onboarding，不参与每月流程）。

对应需求「每个司机工资计算方式不一样，参考历史工资表」：
  逐份读取历史工资 xlsx -> 定位工资列 -> 抽出原 Excel 公式 -> 把单元格引用
  按该司机自己的表头映射成标准字段 -> 生成候选规则 JSON。

铁律：
  * 候选规则一律 confirmed=false，必须经发起人逐条确认后才可用于发薪。
  * 只读历史文件，绝不修改；读不懂（数组公式 / 多义表头 / 歧义汇总块）就标
    needs_manual，绝不猜算法。
  * 产出应写入 config/local/（已被 .gitignore 排除），不进开源仓库。
"""
from __future__ import annotations

import glob
import json
import os
import re
from datetime import date, datetime

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

EXCLUDE_SHEET = ("路线", "车牌", "参数", "说明", "汇总")
WAGE_HEADERS = ("工资", "工资（元）", "工资(元)")
KM_HEADERS = ("公里数", "公里", "运输总公里数")
ROUTE_HEADERS = ("路线", "运输地点", "目的地")
DRIVER_HEADERS = ("名字", "司机", "姓名")


def _text(v):
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).strip()


def _header_fields(ws) -> dict[int, str]:
    """第 1 行表头 -> 列号: 标准字段。返回空表示布局无法识别。"""
    out = {}
    for c in range(1, min(ws.max_column, 20) + 1):
        h = _text(ws.cell(1, c).value).split("\n")[0]
        if not h:
            continue
        if any(k in h for k in WAGE_HEADERS) and "路线" not in h and "结算" not in h:
            out.setdefault(c, "wage")
        elif any(k in h for k in KM_HEADERS):
            out.setdefault(c, "km")
        elif any(k in h for k in ROUTE_HEADERS):
            out.setdefault(c, "route")
        elif any(k in h for k in DRIVER_HEADERS):
            out.setdefault(c, "driver")
        elif "日期" in h:
            out.setdefault(c, "date")
        elif "车牌" in h:
            out.setdefault(c, "plate")
        elif "趟" in h:
            out.setdefault(c, "trip_no")
        elif "业务" in h or "委托方" in h or "项目" in h:
            out.setdefault(c, "business")
    return out


def _pick_month_sheet(wb, preferred=None):
    names = [n for n in wb.sheetnames if not any(x in n for x in EXCLUDE_SHEET)]
    if preferred and preferred in names:
        return preferred
    def key(n):
        m = re.search(r"(\d{4})[.\-年](\d{1,2})", n)
        if m:
            return (int(m.group(1)), int(m.group(2)))
        m = re.match(r"(\d{1,2})月", n)
        if m:
            return (9999, int(m.group(1)))
        return (0, 0)
    return sorted(names, key=key)[-1] if names else None


def _cell_map(formula: str, fields: dict[int, str]) -> dict:
    """把公式中的 G2 / $J2 之类引用映射为 {引用: 标准字段名}。"""
    out = {}
    for col, ref in re.findall(r"\$?([A-Z]{1,3})\$?(\d{1,7})\b", formula, re.I):
        from openpyxl.utils import column_index_from_string
        idx = column_index_from_string(col.upper())
        field = fields.get(idx)
        if field:
            out[f"{col.upper()}{ref}"] = field
    return out


def _route_table(wb):
    for n in wb.sheetnames:
        if "路线" in n and "车牌" in n:
            ws = wb[n]
            table = {}
            for row in ws.iter_rows(min_row=2, values_only=True):
                if len(row) >= 3 and isinstance(row[1], str) and isinstance(row[2], (int, float)):
                    table[row[1].strip()] = row[2]
            return table
    return {}


def learn(wage_dir: str, out_path: str, year: str = "2026",
          month_sheet: str | None = None) -> dict:
    files = sorted(glob.glob(os.path.join(wage_dir, "*.xlsx")))
    if not files:
        raise ValueError(f"目录下没有 xlsx: {wage_dir}")
    drivers, maps, warnings, tables = {}, {}, [], {}
    for path in files:
        fname = os.path.basename(path)
        driver = re.sub(r"\d{4}\.xlsx$", "", fname)
        if not driver:
            continue
        wb = load_workbook(path, data_only=False)
        sheet = _pick_month_sheet(wb, month_sheet)
        if not sheet:
            warnings.append(f"{fname}: 找不到月份 sheet")
            wb.close()
            continue
        ws = wb[sheet]
        fields = _header_fields(ws)
        wage_cols = [c for c, f in fields.items() if f == "wage"]
        if len(wage_cols) != 1:
            warnings.append(f"{fname}/{sheet}: 工资列命中 {len(wage_cols)} 个，需人工指定")
            wb.close()
            continue
        wage_col = wage_cols[0]
        # 找第一个有内容的数据行
        raw = None
        for r in range(2, min(ws.max_row, 12) + 1):
            v = ws.cell(r, wage_col).value
            if v is not None:
                raw = v
                break
        entry = {"confirmed": False, "needs_confirmation": True,
                 "source_file": fname, "source_sheet": sheet,
                 "source_cell": f"{get_column_letter(wage_col)}2"}
        if isinstance(raw, str) and raw.startswith("="):
            if "VLOOKUP" in raw.upper():
                tname = f"route_price_{driver}"
                tables[tname] = _route_table(wb)
                entry["wage"] = {"op": "lookup", "key": "route",
                                 "table": tname, "default": 0}
                entry["note"] = "原公式为 VLOOKUP 路线价目表；已转为 lookup，需确认价目表完整"
            elif any(f in raw.upper() for f in ("IF(", "ROUNDDOWN(", "ROUND(")):
                cmap = _cell_map(raw, fields)
                unmapped = [ref for ref in re.findall(
                    r"\$?[A-Z]{1,3}\$?\d{1,7}\b", raw, re.I) if ref not in cmap]
                entry["wage"] = {"op": "formula", "expr": raw, "cell_map": cmap}
                if unmapped:
                    entry["needs_manual"] = f"存在未映射引用: {unmapped[:6]}"
            else:
                entry["needs_manual"] = f"公式形态未支持: {raw[:80]}"
            entry["source_formula"] = raw
        elif isinstance(raw, (int, float)):
            entry["wage"] = {"op": "const", "value": raw}
        else:
            entry["needs_manual"] = "工资单元格是数组公式或空值，openpyxl 无法读取；需人工补录"
        # 表内姓名与文件名不一致要报警（真实环境常见）
        dcol = next((c for c, f in fields.items() if f == "driver"), None)
        if dcol:
            for r in range(2, min(ws.max_row, 8) + 1):
                v = _text(ws.cell(r, dcol).value)
                if v and v != driver:
                    warnings.append(f"{fname}: 表内姓名 {v!r} 与文件名 {driver!r} 不一致，请补 driver_aliases")
                    break
        drivers[driver] = entry
        maps[driver] = {"file": fname, "sheet": sheet, "header_row": 1,
                        "columns": {f: get_column_letter(c) for c, f in fields.items()}}
        wb.close()

    doc = {"version": 1, "precision": 2, "currency": "CNY",
           "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
           "source": wage_dir, "tables": tables, "drivers": drivers,
           "warnings": warnings,
           "note": "候选规则全部未确认。逐条与历史工资核对后，把 confirmed 改为 true 并填 confirmed_by/confirmed_at；未确认的规则会被 verify 阻断，绝不用于发薪。"}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    map_out = os.path.join(os.path.dirname(out_path), "field_map.candidates.json")
    with open(map_out, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "per_driver_layouts": maps, "warnings": warnings},
                  f, ensure_ascii=False, indent=2)
        f.write("\n")
    return {"out": out_path, "field_map_candidates": map_out,
            "drivers": len(drivers),
            "manual": [k for k, v in drivers.items() if v.get("needs_manual")],
            "warnings": warnings,
            "note": "候选规则未确认，verify 会阻断；请人工确认后再启用。"}
