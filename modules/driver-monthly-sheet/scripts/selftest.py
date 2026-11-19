#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selftest.py —— 内置脱敏样例端到端自测，全部在临时目录进行，绝不动真实表。

覆盖：
1) 模板解析：表头/样式/列宽/合并单元格/冻结
2) 数据源分组：单表+司机列，按姓名分组
3) 报销聚合：ledger sqlite 按 driver+month
4) 生成新 sheet 并精确插入，原有 sheet 与自定义部件（模拟 customXml）零改动
5) 幂等：sheet 已存在时 fill 策略不重建、不重复
6) dry-run 不写文件
"""
from __future__ import annotations

import os
import sys
import json
import shutil
import tempfile
import sqlite3
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pipeline  # noqa: E402
from xlsxzip import list_sheets, _read_parts  # noqa: E402

HEADERS = ["日期", "业务", "车牌", "司机", "趟次", "路线", "公里数", "工资", "路线结算"]


def _make_template(path):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "月度模板"
    ws.append(HEADERS)
    for c in range(1, len(HEADERS) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF", name="苹方-简", size=12)
        cell.fill = PatternFill("solid", fgColor="4472C4")
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = Border(bottom=Side(style="thin", color="000000"))
    ws.append(["示例日期", "宝旺", "粤A00001", "示例司机", 1, "示例路线", 100, 150, 150])
    for c in range(1, len(HEADERS) + 1):
        ws.cell(row=2, column=c).font = Font(name="苹方-简", size=11)
    ws.column_dimensions["A"].width = 14
    ws.column_dimensions["F"].width = 30
    ws.merge_cells("I1:I1")
    ws.freeze_panes = "A2"
    wb.save(path)


def _make_driver_report(path, driver, year):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "%d月" % (year % 12 or 12)
    ws.append(HEADERS)
    ws.append(["2026-11-01", "宝旺", "粤A00001", driver, 1, "示例路线", 100, 150, 150])
    wb.save(path)
    # 注入一个模拟 customXml 部件 + 目录条目，验证 zip 插入不破坏它
    marker = b"SAMPLE-CUSTOM-XML-DO-NOT-LOSE"
    tmp = path + ".tmp"
    with zipfile.ZipFile(path, "r") as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            zout.writestr(info, zin.read(info.filename))
        zout.writestr("customXml/item1.xml", marker)
    shutil.move(tmp, path)


def _make_source(path):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "运输数据"
    ws.append(["日期", "业务", "车牌号", "司机姓名", "车次", "运输地点", "里程", "工资（元）"])
    ws.append(["2026-11-02", "伟豪", "粤B12345", "张司机", 2, "广州-深圳", 180, 260])
    ws.append(["2026-11-03", "宝旺", "粤A11111", "张司机", 1, "中山往返", 90, 150])
    ws.append(["2026-11-04", "伟豪", "粤B99999", "李司机", 1, "东莞往返", 120, 180])
    wb.save(path)


def _make_ledger(path):
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE reimbursements (
        id INTEGER PRIMARY KEY, driver TEXT, amount REAL, pay_time TEXT,
        status TEXT, business_key TEXT)""")
    conn.executemany("INSERT INTO reimbursements(driver,amount,pay_time,status,business_key) VALUES(?,?,?,?,?)", [
        ("张司机", 66.83, "2026-11-15 10:00:00", "reimbursed", "k1"),
        ("张司机", 23.0, "2026-11-16 11:00:00", "pending_payment", "k2"),
        ("张司机", 88.0, "2026-10-15 10:00:00", "reimbursed", "k3"),  # 上月，应排除
        ("李司机", 15.5, "2026-11-17 09:00:00", "reimbursed", "k4"),
    ])
    conn.commit()
    conn.close()


def run_selftest():
    checks = []
    tmpdir = tempfile.mkdtemp(prefix="driver-monthly-sheet-selftest-")
    try:
        data = os.path.join(tmpdir, "data")
        os.makedirs(data)
        template = os.path.join(data, "模板.xlsx")
        source = os.path.join(data, "数据源.xlsx")
        ledger = os.path.join(data, "ledger.sqlite")
        tdir = os.path.join(data, "2026年", "司机工资")
        os.makedirs(tdir)
        _make_template(template)
        _make_source(source)
        _make_ledger(ledger)
        _make_driver_report(os.path.join(tdir, "张司机2026.xlsx"), "张司机", 2026)
        _make_driver_report(os.path.join(tdir, "李司机2026.xlsx"), "李司机", 2026)

        cfg = {
            "version": 1,
            "paths": {"workdir": os.path.join(data), "template": template, "source": source,
                      "target_dir": tdir, "target_file": "{target_dir}/{driver}{year}.xlsx",
                      "backup_dir": os.path.join(data, "_backup"), "state_dir": os.path.join(data, "_state")},
            "template": {"sheet_index": 0, "header_row": 1, "style_from_row": 2, "data_start_row": 2,
                         "copy_column_widths": True, "copy_merged_cells": True, "copy_freeze_panes": True,
                         "column_map": {
                             "日期": "source.日期", "业务": "source.业务", "车牌": "source.车牌号",
                             "司机": "source.司机姓名", "趟次": "source.车次", "路线": "source.运输地点",
                             "公里数": "source.里程", "工资": "source.工资（元）", "路线结算": "blank"}},
            "source": {"sheet": None, "header_row": 1, "data_start_row": 2,
                       "driver_column": ["司机姓名", "司机", "姓名"],
                       "driver_aliases": {},
                       "column_aliases": {"日期": ["日期"], "业务": ["业务"], "车牌": ["车牌", "车牌号"],
                                          "趟次": ["趟次", "车次"], "路线": ["路线", "运输地点"],
                                          "公里数": ["公里数", "里程"], "工资": ["工资", "工资（元）"]},
                       "skip_empty_rows": True},
            "reimbursement": {"mode": "ledger", "ledger_db": ledger, "placement": "columns",
                              "month_source": ["pay_time"], "status_filter": ["reimbursed", "pending_payment"]},
            "sheet": {"name_pattern": "{year}年{month}月", "exist_policy": "fill", "month_zero_pad": False},
            "drivers": {"from": "target_dir", "list": [], "exclude": [], "name_normalize": "strip"},
            "run": {"dry_run": False, "backup_before_write": True, "verify_after_write": True,
                    "on_missing_target": "skip_and_report", "on_unknown_driver": "skip_and_report"}
        }

        # --- check 1: plan (dry-run) 不写文件 ---
        plan_cfg = json.loads(json.dumps(cfg)); plan_cfg["run"]["dry_run"] = True
        plan = pipeline.run(plan_cfg, 2026, 11, tmpdir)
        checks.append(("plan 解析成功", plan.get("ok") and len(plan["sheets"]) == 2, plan.get("errors")))
        checks.append(("plan 识别出 2 名司机", len(plan.get("source", {}).get("driver_count", 0).__str__()) >= 0 and
                       plan["source"]["driver_count"] == 2, plan.get("source")))
        checks.append(("dry-run 未真正写入", "2026年11月" not in list_sheets(os.path.join(tdir, "张司机2026.xlsx")), None))

        # --- check 2: 真实写入 ---
        res = pipeline.run(cfg, 2026, 11, tmpdir)
        checks.append(("run 成功", res.get("ok"), res.get("errors")))
        checks.append(("生成了 2 张 sheet", len(res.get("sheets", [])) == 2, res.get("sheets")))

        zhang = os.path.join(tdir, "张司机2026.xlsx")
        sheets = list_sheets(zhang)
        checks.append(("新 sheet 名字正确(2026年11月)", "2026年11月" in sheets, sheets))
        checks.append(("原有月份 sheet 保留", "10月" in sheets, sheets))

        parts = _read_parts(zhang)
        checks.append(("自定义部件 customXml 零改动", parts.get("customXml/item1.xml") == b"SAMPLE-CUSTOM-XML-DO-NOT-LOSE", parts.get("customXml/item1.xml")))

        import openpyxl
        wb = openpyxl.load_workbook(zhang, data_only=True)
        ws = wb["2026年11月"]
        hdr = [ws.cell(row=1, column=c).value for c in range(1, 10)]
        checks.append(("表头与模板完全一致", hdr == HEADERS, hdr))
        r2 = [ws.cell(row=2, column=c).value for c in range(1, 10)]
        r3 = [ws.cell(row=3, column=c).value for c in range(1, 10)]
        checks.append(("张司机 2 行数据填入", r2[3] == "张司机" and r3[3] == "张司机", (r2, r3)))
        checks.append(("字段映射正确(车牌号->车牌)", r2[2] == "粤B12345", r2))
        checks.append(("数据行数=2(无多余空行)", ws.max_row == 3, ws.max_row))
        wb.close()

        # --- check 3: 报销聚合（张司机 11 月 2 笔 ¥89.83）---
        from reimb_reader import read_reimbursement
        rr = read_reimbursement(cfg, 2026, 11, ["张司机", "李司机"])
        z = rr["data"].get("张司机", {})
        checks.append(("报销按月过滤且聚合正确", z.get("count") == 2 and float(z.get("amount", 0)) == 89.83, z))

        # --- check 4: 幂等（再跑一次，fill 不重建）---
        before = _read_parts(zhang)[_sheet_part_of(zhang, "2026年11月")]
        res2 = pipeline.run(cfg, 2026, 11, tmpdir)
        after = _read_parts(zhang)[_sheet_part_of(zhang, "2026年11月")]
        skipped = any(s.get("reason", "").find("fill") >= 0 or s.get("reason", "").find("已存在") >= 0
                      for s in res2.get("skipped", []))
        checks.append(("幂等：sheet 已存在不重建", skipped and before == after,
                       {"skipped": res2.get("skipped"), "same": before == after}))

        # --- check 5: 无数据源时生成空 sheet（框架）---
        cfg2 = json.loads(json.dumps(cfg))
        cfg2["paths"]["source"] = os.path.join(data, "不存在的数据源.xlsx")
        w2 = os.path.join(tdir, "王司机2026.xlsx")
        _make_driver_report(w2, "王司机", 2026)
        res3 = pipeline.run(cfg2, 2026, 12, tmpdir)
        checks.append(("无数据源仍生成空 sheet", any(s.get("driver") == "王司机" for s in res3.get("sheets", [])),
                       res3.get("sheets")))

        passed = sum(1 for _, ok, _ in checks if ok)
        total = len(checks)
        failed = [{"check": n, "detail": d} for n, ok, d in checks if not ok]
        return {"ok": passed == total, "passed": passed, "total": total,
                "tmpdir": tmpdir, "failed": failed,
                "checks": [{"name": n, "ok": ok} for n, ok, _ in checks]}
    except Exception as e:
        import traceback
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "traceback": traceback.format_exc(), "tmpdir": tmpdir}


def _sheet_part_of(path, sheet_name):
    import re
    from xlsxzip import NS_MAIN, _prefix
    from xml.etree import ElementTree as ET
    parts = _read_parts(path)
    wb = ET.fromstring(parts["xl/workbook.xml"])
    sheets = wb.find(_prefix("sheets", NS_MAIN))
    rid = None
    for sh in sheets.findall(_prefix("sheet", NS_MAIN)):
        if sh.get("name") == sheet_name:
            rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
    rels = ET.fromstring(parts["xl/_rels/workbook.xml.rels"])
    for rel in rels:
        if rel.get("Id") == rid:
            return "xl/" + rel.get("Target")
    return None


if __name__ == "__main__":
    print(json.dumps(run_selftest(), ensure_ascii=False, indent=2, default=str))
