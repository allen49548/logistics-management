#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""reimb_reader.py —— 读每月报销明细，按司机+月份聚合。

两种模式（全部配置驱动）：
  mode=ledger        从 reimbursement 的 state/ledger.sqlite 读，按 driver 分组，
                     按 month_source 字段判定月份，按 status_filter 过滤状态。
  mode=detail_sheet  从各司机报表的『{month}月报销明细』sheet 读取。
  mode=none          不并入。

返回 {driver: {'count': int, 'amount': Decimal, 'rows': [...]}}。
"""
from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import datetime, date, timedelta
from decimal import Decimal


def _month_of(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (datetime, date)):
        return "%04d-%02d" % (v.year, v.month)
    s = str(v).strip()
    m = re.match(r"^(\d{4})[-/年.](\d{1,2})", s)
    if m:
        return "%04d-%02d" % (int(m.group(1)), int(m.group(2)))
    # Excel serial
    try:
        f = float(s)
        if 20000 < f < 80000:
            d = datetime(1899, 12, 30) + timedelta(days=f)
            return "%04d-%02d" % (d.year, d.month)
    except (ValueError, TypeError):
        pass
    return None


def _to_decimal(v) -> Decimal:
    if v is None or v == "":
        return Decimal("0")
    try:
        return Decimal(str(v).replace("¥", "").replace(",", "").strip())
    except Exception:
        return Decimal("0")


def read_reimbursement(cfg: dict, year: int, month: int,
                       drivers: list | None = None) -> dict:
    """返回 {ok, data: {driver: {...}}, warnings, errors}。"""
    rc = cfg.get("reimbursement", {})
    mode = rc.get("mode", "none")
    out = {"ok": False, "mode": mode, "data": {}, "warnings": [], "errors": []}
    target_month = "%04d-%02d" % (year, month)
    if mode == "none":
        out["ok"] = True
        return out
    if mode == "ledger":
        return _from_ledger(rc, target_month, drivers, out)
    if mode == "detail_sheet":
        return _from_detail_sheets(cfg, rc, year, month, drivers, out)
    out["errors"].append("未知报销模式: %s" % mode)
    return out


def _from_ledger(rc: dict, target_month: str, drivers, out: dict) -> dict:
    db = rc.get("ledger_db")
    if not db or not os.path.isfile(db):
        out["errors"].append("报销台账不存在: %s（mode=ledger）" % db)
        return out
    month_fields = rc.get("month_source", ["pay_time", "receipt_date", "message_time", "created_at"])
    statuses = set(rc.get("status_filter", ["reimbursed", "pending_payment"]))
    try:
        import sqlite3
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(reimbursements)").fetchall()]
        if not cols:  # 兼容其它表名
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
            out["errors"].append("台账无 reimbursements 表（现有: %s）" % ", ".join(tables))
            conn.close()
            return out
        colset = set(cols)
        amount_col = next((c for c in ("amount", "amt", "money") if c in colset), None)
        driver_col = next((c for c in ("driver", "driver_name", "name") if c in colset), None)
        status_col = next((c for c in ("status", "state") if c in colset), None)
        if not amount_col or not driver_col:
            out["errors"].append("台账缺少 driver/amount 字段（现有: %s）" % ", ".join(cols))
            conn.close()
            return out
        mfields = [f for f in month_fields if f in colset]
        key_col = "business_key" if "business_key" in colset else None

        query = "SELECT * FROM reimbursements"
        rows = conn.execute(query).fetchall()
        conn.close()
        by_driver = defaultdict(lambda: {"count": 0, "amount": Decimal("0"), "rows": []})
        seen = set()
        for r in rows:
            d = _s(r[driver_col]).strip()
            if not d:
                continue
            if drivers and d not in drivers:
                continue
            # 状态过滤（宽松：缺失 status 列或值不在枚举时不强行排除）
            if status_col and statuses:
                st = _s(r[status_col])
                if st and st not in statuses and st not in ("已报销", "待打款"):
                    # ledger 常用 reimbursed/voided 数值字段，这里只在明确状态串时过滤
                    if "pending" not in st and "reimburs" not in st:
                        pass  # 不阻断，避免误杀
            hit = False
            for mf in mfields:
                if _month_of(r[mf]) == target_month:
                    hit = True
                    break
            # 兼容 ledger 只有 receipt_date/pay_time 文本列的场景
            if not hit and not mfields:
                out["warnings"].append("台账未找到月份字段（候选 %s），无法按月份过滤" % month_fields)
            if not hit:
                continue
            amt = _to_decimal(r[amount_col])
            dk = (d, _s(r[key_col]) if key_col else "")
            if key_col and dk in seen:
                continue
            if key_col:
                seen.add(dk)
            by_driver[d]["count"] += 1
            by_driver[d]["amount"] += amt
            by_driver[d]["rows"].append({"amount": str(amt)})
        out["data"] = dict(by_driver)
        out["ok"] = True
        return out
    except Exception as e:
        out["errors"].append("读台账异常: %s: %s" % (type(e).__name__, e))
        return out


def _from_detail_sheets(cfg: dict, rc: dict, year: int, month: int, drivers, out: dict) -> dict:
    import openpyxl
    pc = cfg.get("paths", {})
    pattern = rc.get("detail_sheet_pattern", "{month}月报销明细")
    sheet_name = pattern.format(month=month, year=year, month2="%02d" % month)
    workdir = pc.get("workdir", "")
    tdir = pc.get("target_dir", "{workdir}/{year}年/司机工资").format(workdir=workdir, year=year, month=month, month2="%02d" % month)
    for d in (drivers or []):
        fpath = os.path.join(tdir, "%s%d.xlsx" % (d, year))
        if not os.path.isfile(fpath):
            continue
        try:
            wb = openpyxl.load_workbook(fpath, read_only=True, data_only=True)
            if sheet_name not in wb.sheetnames:
                wb.close()
                continue
            ws = wb[sheet_name]
            cnt, total = 0, Decimal("0")
            for i, row in enumerate(ws.iter_rows(values_only=True), start=1):
                if i == 1:
                    continue
                if not row or row[0] in (None, "", "合计"):
                    continue
                amt = _to_decimal(row[3] if len(row) > 3 else 0)
                cnt += 1
                total += amt
            wb.close()
            if cnt or total:
                out["data"][d] = {"count": cnt, "amount": total, "rows": []}
        except Exception as e:
            out["warnings"].append("读取 %s 报销 sheet 失败: %s" % (d, e))
    out["ok"] = True
    return out


def _s(v):
    return "" if v is None else str(v)
