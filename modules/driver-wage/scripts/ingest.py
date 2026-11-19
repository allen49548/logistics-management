#!/usr/bin/env python3
"""输入适配层：把运输单明细 / 报销数据 / 历史工资 / 外部账单读成标准结构。

契约（重要）：
  * 图片类运输单先由 WorkBuddy 的视觉能力逐行转录成 CSV/JSON（保留行列位置），
    本模块只消费结构化文件；不内置任何付费或私有 OCR 依赖。
  * 所有读取均为只读。绝不修改用户桌面上的历史工资表或报销台账。
  * 司机姓名无法映射到名册时保留原值并标记 unresolved，由 verify 阻断——绝不猜归属。
  * 列映射支持三种写法：表头名 / Excel 列字母 / 0-based 数字，适配每位司机不同的表头布局。
"""
from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from config import ConfigError, expand
from rules import dec

DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%m月%d日")


def _as_text(v) -> str:
    return "" if v is None else str(v).strip()


def column_index(spec, headers: list[str]) -> int:
    """把列映射解析为 0-based 索引。"""
    if isinstance(spec, int):
        return spec
    s = _as_text(spec)
    if s.isdigit():
        return int(s)
    # 表头名优先：否则 "km" 会被误判成 Excel 列 KM，"date" 之类同理
    for i, h in enumerate(headers):
        if _as_text(h) == s:
            return i
    if re.fullmatch(r"[A-Za-z]{1,3}", s):
        from openpyxl.utils import column_index_from_string
        return column_index_from_string(s.upper()) - 1
    raise ConfigError(f"找不到列 {s!r}；可用表头: {headers[:15]}")


def _read_rows(path: str, source: dict) -> tuple[list[str], list[list]]:
    """返回 (表头行, 数据行)。xlsx 支持单 sheet / '*' 遍历 / sheet 名含月份。"""
    kind = source.get("type") or Path(path).suffix.lower().lstrip(".")
    header_row = int(source.get("header_row", 1))
    if kind in ("json",):
        with open(path, "r", encoding="utf-8") as f:
            doc = json.load(f)
        rows = doc.get("rows", doc) if isinstance(doc, dict) else doc
        if rows and isinstance(rows[0], dict):
            keys = list(rows[0].keys())
            return keys, [[r.get(k) for k in keys] for r in rows]
        return (rows[0] if rows else []), rows[1:]
    if kind in ("csv", "tsv"):
        delim = "\t" if kind == "tsv" else ","
        with open(path, "r", encoding=source.get("encoding", "utf-8-sig"), newline="") as f:
            rows = [list(r) for r in csv.reader(f, delimiter=delim)]
    elif kind in ("xlsx", "xlsm"):
        from openpyxl import load_workbook
        wb = load_workbook(path, data_only=True, read_only=True)
        wanted = source.get("sheet")
        names = wb.sheetnames if wanted in (None, "", "*") else [wanted]
        rows = []
        for name in names:
            if name not in wb.sheetnames:
                continue
            ws = wb[name]
            # 真实报表常被格式撑到 1048576 行 / 16384 列，必须封顶，
            # 否则只读遍历也会耗时数分钟并吃满内存。
            max_r = min(ws.max_row, int(source.get("max_row", 20000)))
            max_c = min(ws.max_column, int(source.get("max_col", 30)))
            for r in ws.iter_rows(min_row=1, max_row=max_r, max_col=max_c,
                                  values_only=True):
                rows.append(list(r))
        wb.close()
    else:
        raise ConfigError(f"不支持的来源类型 {kind!r}: {path}")
    headers = rows[header_row - 1] if len(rows) >= header_row else []
    return [_as_text(h) for h in headers], rows[header_row:]


def parse_date(value, year_hint: int | None = None) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        try:
            from openpyxl.utils.datetime import from_excel
            d = from_excel(float(value))
            return d.date().isoformat() if isinstance(d, datetime) else d.isoformat()
        except Exception:
            pass
    s = _as_text(value)
    for fmt in DATE_PATTERNS:
        try:
            d = datetime.strptime(s, fmt)
            if "%Y" not in fmt and year_hint:
                d = d.replace(year=year_hint)
            return d.date().isoformat()
        except ValueError:
            continue
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-01"
    return None


def normalize_driver(raw: str, cfg: dict) -> tuple[str, str, bool]:
    aliases = {**cfg["field_map"].get("driver_aliases", {}),
               **{d["name"]: d["id"] for d in cfg["roster"].get("drivers", [])
                  if isinstance(d, dict) and "id" in d and "name" in d}}
    key = _as_text(raw)
    did = aliases.get(key)
    if did:
        names = {d["id"]: d.get("name", d["id"]) for d in cfg["roster"]["drivers"]}
        return did, names.get(did, key), True
    return key, key, False


def _open_source(path: str, source: dict, cfg: dict) -> tuple[list[str], list[list]]:
    full = expand(path, cfg["payroll"], {"root": cfg["_meta"]["root"],
                                         "profile": cfg["_meta"]["profile"]})
    # {data_dir} 等自定义占位符
    full = expand(full, {"paths": cfg["paths"]}, cfg["_meta"])
    if not os.path.isfile(full):
        raise ConfigError(f"输入文件不存在: {full}")
    return _read_rows(full, source), full


def expand_source_path(path: str, month: str) -> str:
    """展开按月命名占位符：{month} {M} {MM} {year} {YYYY}。"""
    y, m = int(month[:4]), int(month[5:7])
    return (path.replace("{month}", month).replace("{MM}", f"{m:02d}")
            .replace("{M}", str(m)).replace("{YYYY}", str(y))
            .replace("{year}", str(y)))


def load_trips(cfg: dict, month: str) -> tuple[list[dict], list[str], list[str]]:
    """读取并筛选指定月份的运输单明细。返回 (标准明细, 警告, 源文件路径)。"""
    trips, warnings, used = [], [], []
    fm = cfg["field_map"]
    for source in fm.get("trips_sources", []):
        if not source.get("enabled", True):
            continue
        try:
            (headers, rows), full = _open_source(
                expand_source_path(source["path"], month), source, cfg)
        except ConfigError as exc:
            warnings.append(f"来源 {source.get('id')} 跳过: {exc}")
            continue
        used.append(full)
        cols = {k: column_index(v, headers) for k, v in source["columns"].items()}
        require = source.get("filters", {}).get("require", ["driver", "date", "km"])
        km_min = dec(source.get("filters", {}).get("km_min", 0))
        km_max = dec(source.get("filters", {}).get("km_max", 999999))
        # 真实运输单常混有大量外部车辆/司机；开启后只保留名册内司机，
        # 这是"筛选数据"的主要手段，且过滤数量必须透明记录。
        exclude_unmapped = bool(source.get("filters", {})
                                .get("exclude_unmapped_drivers", False))
        filtered_out = 0
        year_hint = int(month[:4])
        for n, row in enumerate(rows, start=int(source.get("header_row", 1)) + 1):
            def get(field):
                i = cols.get(field)
                return row[i] if i is not None and i < len(row) else None
            raw_driver = _as_text(get("driver"))
            raw_date = get("date")
            km_raw = get("km")
            if all(_as_text(get(f)) in ("", "None") for f in ("driver", "date", "km", "route")):
                continue
            if any(_as_text(get(f)) == "" for f in require):
                warnings.append(f"{os.path.basename(full)}:{n} 缺少必填字段 {require}，跳过")
                continue
            d = parse_date(raw_date, year_hint)
            if not d:
                warnings.append(f"{os.path.basename(full)}:{n} 日期无法解析 {raw_date!r}，跳过")
                continue
            row_month = d[:7]
            if row_month != month:
                continue
            did, display, resolved = normalize_driver(raw_driver, cfg)
            if not resolved and exclude_unmapped:
                filtered_out += 1
                continue
            if not resolved:
                warnings.append(f"{os.path.basename(full)}:{n} 司机 {raw_driver!r} 不在名册/别名中（unresolved）")
            try:
                km = dec(km_raw)
            except Exception:
                warnings.append(f"{os.path.basename(full)}:{n} 公里数非法 {km_raw!r}，跳过")
                continue
            if km < km_min or km > km_max:
                warnings.append(f"{os.path.basename(full)}:{n} 公里数 {km} 越界，跳过")
                continue
            trips.append({
                "date": d, "month": row_month, "driver": did,
                "display_name": display, "driver_raw": raw_driver,
                "driver_resolved": resolved, "plate": _as_text(get("plate")),
                "business": _as_text(get("business")),
                "trip_no": _as_text(get("trip_no")), "route": _as_text(get("route")),
                "km": str(km), "external_fee": _as_text(get("external_fee")),
                "source": os.path.basename(full), "source_row": n,
                "source_id": source.get("id", "trips"),
            })
        if filtered_out:
            warnings.append(f"{os.path.basename(full)}: 已按名册筛除 {filtered_out} 行外部司机运输单")
    return trips, warnings, used


def load_reimbursements(cfg: dict, month: str) -> tuple[dict[str, Decimal], list[str], list[str]]:
    """报销数据：csv/xlsx（按司机+月份汇总）或 sqlite 台账（强制只读）。"""
    spec = cfg["field_map"].get("reimbursement")
    out: dict[str, Decimal] = {}
    warnings, used = [], []
    if not spec or not spec.get("enabled", True):
        return out, warnings, used
    kind = spec.get("type", "csv")
    path = expand(spec["path"], cfg["payroll"], cfg["_meta"])
    path = expand(path, {"paths": cfg["paths"]}, cfg["_meta"])
    if not os.path.isfile(path):
        return out, [f"报销数据不存在: {path}"], used
    used.append(path)
    if kind == "sqlite":
        # uri mode=ro 保证绝不改动已有报销台账
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            sql = spec["query"]
            for raw_driver, m, amount in con.execute(sql):
                if str(m)[:7] != month:
                    continue
                did, _, ok = normalize_driver(_as_text(raw_driver), cfg)
                if not ok:
                    warnings.append(f"报销记录司机 {raw_driver!r} 未映射，未计入（需人工确认）")
                    continue
                out[did] = out.get(did, Decimal(0)) + dec(amount)
        finally:
            con.close()
    else:
        (headers, rows), _ = _read_rows(path, spec), path
        cols = {k: column_index(v, headers) for k, v in spec["columns"].items()}
        for n, row in enumerate(rows, start=int(spec.get("header_row", 1)) + 1):
            def get(f):
                i = cols.get(f)
                return row[i] if i is not None and i < len(row) else None
            if _as_text(get("driver")) == "":
                continue
            m = _as_text(get("month"))
            if m and m[:7] != month:
                continue
            did, _, ok = normalize_driver(_as_text(get("driver")), cfg)
            if not ok:
                warnings.append(f"报销行 {n} 司机 {get('driver')!r} 未映射，未计入")
                continue
            out[did] = out.get(did, Decimal(0)) + dec(get("amount"))
    return out, warnings, used


def load_history(cfg: dict, month: str) -> tuple[dict[str, Decimal], list[str]]:
    """历史工资（环比基线）：driver_id -> 上月实发合计。缺失不算错误。"""
    spec = cfg["field_map"].get("history")
    out: dict[str, Decimal] = {}
    if not spec or not spec.get("enabled", True):
        return out, []
    path = expand(spec["path"], cfg["payroll"], cfg["_meta"])
    path = expand(path, {"paths": cfg["paths"]}, cfg["_meta"])
    if not os.path.isfile(path):
        return out, [f"历史工资文件不存在（环比检查降级）: {path}"]
    (headers, rows), _ = _read_rows(path, spec), path
    cols = {k: column_index(v, headers) for k, v in spec["columns"].items()}
    for row in rows:
        def get(f):
            i = cols.get(f)
            return row[i] if i is not None and i < len(row) else None
        if _as_text(get("driver")) == "" or _as_text(get("month"))[:7] != month:
            continue
        did, _, ok = normalize_driver(_as_text(get("driver")), cfg)
        if ok:
            out[did] = out.get(did, Decimal(0)) + dec(get("net"))
    return out, []
